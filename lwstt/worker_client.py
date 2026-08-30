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
        # Set when a write fails part-way. Anything sent afterwards would be
        # read as a continuation of the half-written frame.
        self._stream_broken = False
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
        self._stream_broken = False
        self._reader = threading.Thread(target=self._read_loop, name="lwstt-worker-out", daemon=True)
        self._reader.start()
        self._stderr_pump = threading.Thread(
            target=self._stderr_loop, name="lwstt-worker-err", daemon=True
        )
        self._stderr_pump.start()
        self.last_activity = time.monotonic()

    def stop(self, timeout: float = 3.0) -> None:
        """Ask politely, then insist."""
        process = self._process
        if process is None or process.poll() is not None:
            with self._write_lock:
                self._process = None
            self.models_loaded = False
            return
        try:
            self._send(Msg.SHUTDOWN)
            process.wait(timeout=timeout)
        except Exception:
            pass
        if process.poll() is None:
            process.kill()
        # Under the lock: a sender mid-write must not find _process swapped out
        # from under it, nor start writing to a process that is being torn down.
        with self._write_lock:
            self._process = None
            self._stream_broken = False
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

    def _write(self, framed: bytes) -> bool:
        """The single path to the worker's stdin.

        Everything is done under the lock, including looking up the process, so
        a concurrent stop() cannot swap it out mid-frame. A failed write poisons
        the stream rather than being retried: a write that fails part-way leaves
        an incomplete frame in the pipe, and every byte sent after it is read as
        a continuation of that frame. The worker then decodes raw PCM as a
        header, hits a zero type byte, and dies -- observed in the wild as
        "unknown message type 0".
        """
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                return False
            if self._stream_broken:
                return False
            try:
                process.stdin.write(framed)
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._stream_broken = True
                self._log(f"write failed, stream poisoned ({exc}); worker needs a restart")
                return False
            self.last_activity = time.monotonic()
            return True

    def _send(self, msg: Msg, payload: bytes = b"") -> bool:
        return self._write(encode(msg, payload))

    # -- protocol --------------------------------------------------------

    def start_dictation(self, settings: dict) -> bool:
        # A poisoned stream cannot be recovered by writing to it; replace the
        # process so the next dictation starts from a clean pipe.
        if self._stream_broken:
            self._log("restarting the worker: previous stream was poisoned")
            self.stop()
        if not self.alive:
            self.start()
        try:
            payload = encode_json(Msg.START, settings)
        except Exception as exc:
            self._log(f"could not encode settings: {exc}")
            return False
        return self._write(payload)



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
