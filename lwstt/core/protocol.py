"""Supervisor <-> worker framing.

Length-prefixed messages over a pipe. No sockets, no third-party IPC, nothing
the supervisor would have to import a package for.

Wire format, per message:

    1 byte   message type
    4 bytes  payload length, big-endian unsigned
    N bytes  payload

Audio payloads are raw little-endian int16 PCM. Everything else is UTF-8 JSON.

Every JSON message from the worker carries the ``session`` id it belongs to.
Without it, a PREVIEW or COMMIT produced for one dictation can arrive after the
next has started and be typed into it -- which looks like the previous
dictation "filling itself in" on the following keypress.
"""

from __future__ import annotations

import json
import struct
from enum import IntEnum
from typing import Any, Iterator

HEADER = struct.Struct(">BI")
HEADER_SIZE = HEADER.size

# A single frame should never approach this. It exists so a desynchronised or
# corrupt stream fails loudly instead of trying to allocate multiple gigabytes.
MAX_FRAME_BYTES = 64 * 1024 * 1024


class Msg(IntEnum):
    # supervisor -> worker
    START = 1  # begin a dictation; payload carries the resolved settings
    AUDIO = 2  # raw int16 PCM
    END = 3  # hotkey released; finish the tail chunk
    ABORT = 4  # discard everything for this dictation
    SHUTDOWN = 5
    PING = 6

    # worker -> supervisor
    PREVIEW = 20  # provisional text for the current chunk
    COMMIT = 21  # a chunk is final
    DONE = 22  # dictation complete
    ERROR = 23
    READY = 24  # models loaded and usable
    PONG = 25
    PENDING = 26  # whether speech is buffered that has not been committed yet


class ProtocolError(Exception):
    pass


def encode(msg: Msg, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(f"payload of {len(payload)} bytes exceeds the frame limit")
    return HEADER.pack(int(msg), len(payload)) + payload


def encode_json(msg: Msg, obj: Any) -> bytes:
    return encode(msg, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def decode_json(payload: bytes) -> Any:
    return json.loads(payload.decode("utf-8"))


class FrameReader:
    """Incremental frame parser.

    Feed it whatever arrives from the pipe, in any size; it yields complete
    frames only. Pipes split writes at arbitrary boundaries, so this must never
    assume one read equals one message.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[tuple[Msg, bytes]]:
        self._buf.extend(data)
        while True:
            if len(self._buf) < HEADER_SIZE:
                return
            raw_type, length = HEADER.unpack_from(self._buf, 0)
            if length > MAX_FRAME_BYTES:
                raise ProtocolError(f"frame claims {length} bytes; stream is corrupt")
            total = HEADER_SIZE + length
            if len(self._buf) < total:
                return
            payload = bytes(self._buf[HEADER_SIZE:total])
            del self._buf[:total]
            try:
                msg = Msg(raw_type)
            except ValueError as exc:
                raise ProtocolError(f"unknown message type {raw_type}") from exc
            yield msg, payload

    @property
    def buffered(self) -> int:
        """Bytes held pending a complete frame. Should stay small."""
        return len(self._buf)
