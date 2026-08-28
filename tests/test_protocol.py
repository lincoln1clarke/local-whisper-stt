"""Tests for supervisor <-> worker framing.

Pipes split writes at arbitrary boundaries, so the reader must never assume one
read equals one message. Audio streams at 32 KB/s for as long as the user talks,
so a framing bug here surfaces as silent corruption minutes in.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lwstt.core.protocol import (
    HEADER_SIZE,
    MAX_FRAME_BYTES,
    FrameReader,
    Msg,
    ProtocolError,
    decode_json,
    encode,
    encode_json,
)


def read_all(reader, data):
    return list(reader.feed(data))


class TestEncoding:
    def test_round_trip_bytes(self):
        frames = read_all(FrameReader(), encode(Msg.AUDIO, b"\x01\x02\x03"))
        assert frames == [(Msg.AUDIO, b"\x01\x02\x03")]

    def test_round_trip_json(self):
        payload = {"text": "hello", "chunk": 3}
        (msg, raw), = read_all(FrameReader(), encode_json(Msg.PREVIEW, payload))
        assert msg is Msg.PREVIEW
        assert decode_json(raw) == payload

    def test_empty_payload(self):
        assert read_all(FrameReader(), encode(Msg.END)) == [(Msg.END, b"")]

    def test_header_size(self):
        assert len(encode(Msg.PING)) == HEADER_SIZE

    def test_non_ascii_survives(self):
        payload = {"text": "café — naïve 🙂 ~markers~"}
        (_, raw), = read_all(FrameReader(), encode_json(Msg.COMMIT, payload))
        assert decode_json(raw) == payload

    def test_oversized_payload_is_rejected(self):
        with pytest.raises(ProtocolError):
            encode(Msg.AUDIO, b"x" * (MAX_FRAME_BYTES + 1))


class TestStreaming:
    def test_two_frames_in_one_read(self):
        data = encode(Msg.PING) + encode(Msg.AUDIO, b"abc")
        assert read_all(FrameReader(), data) == [(Msg.PING, b""), (Msg.AUDIO, b"abc")]

    def test_frame_split_across_reads(self):
        data = encode(Msg.AUDIO, b"hello world")
        reader = FrameReader()
        assert read_all(reader, data[:4]) == []
        assert read_all(reader, data[4:9]) == []
        assert read_all(reader, data[9:]) == [(Msg.AUDIO, b"hello world")]

    def test_byte_at_a_time(self):
        """The pathological case a pipe can actually produce."""
        data = encode_json(Msg.COMMIT, {"text": "one two three"})
        reader = FrameReader()
        out = []
        for i in range(len(data)):
            out.extend(reader.feed(data[i : i + 1]))
        assert len(out) == 1
        assert decode_json(out[0][1]) == {"text": "one two three"}

    def test_header_split_mid_length_field(self):
        data = encode(Msg.AUDIO, b"payload")
        reader = FrameReader()
        assert read_all(reader, data[:3]) == []
        assert read_all(reader, data[3:]) == [(Msg.AUDIO, b"payload")]

    def test_many_frames_streamed_in_odd_slices(self):
        expected = [(Msg.AUDIO, bytes([i]) * (i + 1)) for i in range(40)]
        blob = b"".join(encode(m, p) for m, p in expected)
        reader = FrameReader()
        out = []
        for i in range(0, len(blob), 7):
            out.extend(reader.feed(blob[i : i + 7]))
        assert out == expected

    def test_buffer_drains_after_complete_frames(self):
        reader = FrameReader()
        read_all(reader, encode(Msg.AUDIO, b"x" * 100))
        assert reader.buffered == 0

    def test_buffer_holds_only_the_partial_frame(self):
        reader = FrameReader()
        data = encode(Msg.AUDIO, b"x" * 100)
        read_all(reader, data[:-10])
        assert reader.buffered == len(data) - 10


class TestCorruption:
    def test_unknown_message_type_raises(self):
        bad = bytes([99]) + (0).to_bytes(4, "big")
        with pytest.raises(ProtocolError, match="unknown message type"):
            read_all(FrameReader(), bad)

    def test_absurd_length_raises_instead_of_allocating(self):
        bad = bytes([int(Msg.AUDIO)]) + (2**31).to_bytes(4, "big")
        with pytest.raises(ProtocolError, match="corrupt"):
            read_all(FrameReader(), bad)

    def test_a_valid_frame_before_corruption_is_still_delivered(self):
        reader = FrameReader()
        stream = encode(Msg.PING) + bytes([99]) + (0).to_bytes(4, "big")
        out = []
        with pytest.raises(ProtocolError):
            for frame in reader.feed(stream):
                out.append(frame)
        assert out == [(Msg.PING, b"")]


class TestProperties:
    @given(st.lists(st.binary(max_size=200), max_size=15))
    def test_any_sequence_round_trips(self, payloads):
        blob = b"".join(encode(Msg.AUDIO, p) for p in payloads)
        assert read_all(FrameReader(), blob) == [(Msg.AUDIO, p) for p in payloads]

    @given(st.binary(max_size=500), st.integers(min_value=1, max_value=64))
    def test_any_chunking_yields_the_same_frame(self, payload, size):
        data = encode(Msg.AUDIO, payload)
        reader = FrameReader()
        out = []
        for i in range(0, len(data), size):
            out.extend(reader.feed(data[i : i + size]))
        assert out == [(Msg.AUDIO, payload)]

    @given(st.sampled_from(list(Msg)), st.binary(max_size=100))
    def test_every_message_type_round_trips(self, msg, payload):
        assert read_all(FrameReader(), encode(msg, payload)) == [(msg, payload)]
