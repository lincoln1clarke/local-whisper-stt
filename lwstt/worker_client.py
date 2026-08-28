"""Worker process lifecycle, seen from the supervisor.

Spawns the worker on demand, streams audio to it, dispatches its replies, and
kills it when idle. Nothing here imports numpy or faster-whisper -- that is the
whole point of the split, and tests assert it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Callable

from .core.protocol import FrameReader, Msg, decode_json, encode, encode_json

CREATE_NO_WINDOW = 0x08000000


class WorkerClient:
    """A worker subprocess, or the absence of one."""

    def __init__(
        self,
        on_message: Callable[[Msg, dict], None],
        on_exit: Callable[[], None] | None = None,
        python: str | None = None,
        log: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self._on_message = on_message
        self._on_exit = on_exit
        self._python = python or sys.executable
        self._log = log
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr_pump: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.models_loaded = False

    # -- lifecycle -------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self.alive:
            return
        env = dict(os.environ)
        # No network calls by construction, independent of the firewall rule.
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("PYTHONUNBUFFERED", "1")
        self._process = subprocess.Popen(
            [self._python, "-m", "lwstt.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self._reader = threading.Thread(target=self._read_loop, name="lwstt-worker-out", daemon=True)
        self._reader.start()
        self._stderr_pump = threading.Thread(
            target=self._stderr_loop, name="lwstt-worker-err", daemon=True
        )
        self._stderr_pump.start()
        self.last_activity = time.monotonic()

    def stop(self, timeout: float = 3.0) -> None:
        """Ask politely, then insist."""
        if not self.alive:
            self._process = None
            return
        try:
            self._send(Msg.SHUTDOWN)
            self._process.wait(timeout=timeout)
        except Exception:
            pass
        if self._process and self._process.poll() is None:
            self._process.kill()
        self._process = None
        self.models_loaded = False

    # -- io --------------------------------------------------------------

    def _read_loop(self) -> None:
        process = self._process
        reader = FrameReader()
        try:
            while process and process.stdout:
                # read1, not read: read(n) waits for the full n bytes.
                data = process.stdout.read1(4096)
                if not data:
                    break
                for msg, payload in reader.feed(data):
                    try:
                        obj = decode_json(payload) if payload else {}
                    except Exception:
                        obj = {}
                    if msg is Msg.READY:
                        self.models_loaded = True
                    self._on_message(msg, obj)
        except Exception as exc:
            self._log(f"worker read loop ended: {exc}")
        finally:
            if self._on_exit:
                self._on_exit()

    def _stderr_loop(self) -> None:
        process = self._process
        try:
            while process and process.stderr:
                line = process.stderr.readline()
                if not line:
                    break
                self._log(line.decode("utf-8", "replace").rstrip())
        except Exception:
            pass

    def _send(self, msg: Msg, payload: bytes = b"") -> bool:
        if not self.alive or not self._process.stdin:
            return False
        try:
            with self._write_lock:
                self._process.stdin.write(encode(msg, payload))
                self._process.stdin.flush()
            self.last_activity = time.monotonic()
            return True
        except (BrokenPipeError, OSError) as exc:
            self._log(f"worker write failed: {exc}")
            return False

    # -- protocol --------------------------------------------------------

    def start_dictation(self, settings: dict) -> bool:
        if not self.alive:
            self.start()
        try:
            payload = encode_json(Msg.START, settings)
        except Exception as exc:
            self._log(f"could not encode settings: {exc}")
            return False
        return self._send_raw(payload)

    def _send_raw(self, framed: bytes) -> bool:
        if not self.alive or not self._process.stdin:
            return False
        try:
            with self._write_lock:
                self._process.stdin.write(framed)
                self._process.stdin.flush()
            self.last_activity = time.monotonic()
            return True
        except (BrokenPipeError, OSError):
            return False

    def send_audio(self, pcm: bytes) -> bool:
        if not pcm:
            return True
        return self._send(Msg.AUDIO, pcm)

    def end_dictation(self) -> bool:
        return self._send(Msg.END)

    def abort(self) -> bool:
        return self._send(Msg.ABORT)

    def ping(self) -> bool:
        return self._send(Msg.PING)

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity
