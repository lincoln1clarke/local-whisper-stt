"""Control channel between a second launch and the running instance.

There is no UI, so this is the only way to quit, reload or inspect a running
supervisor. It is a Windows named pipe via multiprocessing.connection, which is
stdlib and needs no third-party package.

Only send_bytes/recv_bytes are used. The pickle-based send/recv are deliberately
avoided: deserialising a pickle is arbitrary code execution, and this pipe exists
inside a process that already holds a system-wide keyboard hook.
"""

from __future__ import annotations

import json
import threading
from multiprocessing.connection import Client, Listener
from typing import Callable

ADDRESS = r"\\.\pipe\lwstt-control"
FAMILY = "AF_PIPE"
TIMEOUT = 3.0


def send_command(command: str, address: str = ADDRESS, timeout: float = TIMEOUT) -> dict:
    """Send a command to the running instance and return its reply."""
    conn = Client(address, family=FAMILY)
    try:
        conn.send_bytes(command.encode("utf-8"))
        if conn.poll(timeout):
            return json.loads(conn.recv_bytes().decode("utf-8"))
        return {"ok": False, "error": "timed out waiting for a reply"}
    finally:
        conn.close()


class ControlServer:
    """Accepts one command per connection and replies with JSON."""

    def __init__(
        self,
        handler: Callable[[str], dict],
        address: str = ADDRESS,
        log: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self._handler = handler
        self._address = address
        self._log = log
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._listener = Listener(self._address, family=FAMILY)
        self._thread = threading.Thread(target=self._serve, name="lwstt-control", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except OSError:
                break
            except Exception as exc:
                self._log(f"control accept failed: {exc}")
                continue
            try:
                command = conn.recv_bytes().decode("utf-8", "replace").strip()
                try:
                    reply = self._handler(command)
                except Exception as exc:
                    reply = {"ok": False, "error": str(exc)}
                conn.send_bytes(json.dumps(reply).encode("utf-8"))
            except Exception as exc:
                self._log(f"control connection failed: {exc}")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._listener:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None
