"""Regression tests for microphone capture.

These exist because of a specific bug that produced a completely convincing
failure: the app recorded for the right duration, logged the right file sizes,
showed the recording indicator, and transcribed to nothing at all.

WAVEHDR.lpData had been declared ``c_char_p``. ctypes auto-converts a
``c_char_p`` *struct field* to a Python bytes object on attribute access and
truncates it at the first NUL byte -- and PCM audio is mostly NULs. Reading the
capture buffer back through that field returned arbitrary heap memory, which
looks exactly like loud noise: full-scale peaks, ~85% non-zero samples, and no
speech for the VAD to find.

Nothing about the shape of the data was wrong. Only its content.
"""

from __future__ import annotations

import ctypes
import struct

import pytest

from lwstt.win.audio import WAVEHDR, Recorder


class TestBufferPointerType:
    def test_lpdata_is_not_c_char_p(self):
        """The declaration that caused the bug. Guard it directly."""
        field_type = dict((name, kind) for name, kind in WAVEHDR._fields_)["lpData"]
        assert field_type is not ctypes.c_char_p, (
            "c_char_p struct fields auto-convert and truncate at the first NUL; "
            "PCM audio is full of NULs"
        )

    def test_reading_a_buffer_through_the_header_preserves_nul_bytes(self):
        payload = b"\x01\x00\x02\x00\x00\x00\x03\x00" * 4
        assert b"\x00" in payload

        block = ctypes.create_string_buffer(len(payload) + 1)
        block.raw = payload + b"\x00"
        header = WAVEHDR(
            lpData=ctypes.cast(block, ctypes.POINTER(ctypes.c_char)),
            dwBufferLength=len(payload),
            dwBytesRecorded=len(payload),
        )

        assert block.raw[: len(payload)] == payload
        assert ctypes.string_at(header.lpData, len(payload)) == payload

    def test_the_old_approach_would_have_truncated(self):
        """Pins down exactly what went wrong, so it cannot quietly return."""
        payload = b"\x01\x02\x00\x03\x04"
        block = ctypes.create_string_buffer(payload, len(payload) + 1)

        class Truncating(ctypes.Structure):
            _fields_ = [("lpData", ctypes.c_char_p)]

        bad = Truncating(lpData=ctypes.cast(block, ctypes.c_char_p))
        assert bad.lpData == b"\x01\x02", "c_char_p stops at the first NUL"
        assert bad.lpData != payload


class TestDrainReady:
    """The harvest path, without a microphone."""

    def _prepared(self, recorder, blocks: list[bytes]):
        from lwstt.win.audio import WHDR_DONE

        for data in blocks:
            block = ctypes.create_string_buffer(recorder.bytes_per_buffer)
            block.raw = data.ljust(recorder.bytes_per_buffer, b"\x00")
            header = WAVEHDR(
                lpData=ctypes.cast(block, ctypes.POINTER(ctypes.c_char)),
                dwBufferLength=recorder.bytes_per_buffer,
                dwBytesRecorded=len(data),
                dwFlags=WHDR_DONE,
            )
            recorder._blocks.append(block)
            recorder._headers.append(header)

    def test_drains_exactly_what_was_recorded(self):
        recorder = Recorder()
        payload = b"\x11\x00\x22\x00\x00\x00\x33\x00"
        self._prepared(recorder, [payload])
        assert recorder.drain_ready(requeue=False) == len(payload)
        assert recorder.read_available() == payload

    def test_preserves_order_across_buffers(self):
        recorder = Recorder()
        first, second = b"\x01\x00" * 4, b"\x02\x00" * 4
        self._prepared(recorder, [first, second])
        recorder.drain_ready(requeue=False)
        assert recorder.read_available() == first + second

    def test_ignores_buffers_that_are_not_done(self):
        recorder = Recorder()
        self._prepared(recorder, [b"\x01\x00\x02\x00"])
        recorder._headers[0].dwFlags = 0
        assert recorder.drain_ready(requeue=False) == 0
        assert recorder.read_available() == b""

    def test_clears_the_done_flag_so_a_buffer_is_not_read_twice(self):
        recorder = Recorder()
        self._prepared(recorder, [b"\x01\x00\x02\x00"])
        recorder.drain_ready(requeue=False)
        recorder.read_available()
        assert recorder.drain_ready(requeue=False) == 0
        assert recorder.read_available() == b""

    def test_tracks_seconds_recorded(self):
        recorder = Recorder(sample_rate=16000, buffer_ms=1000)
        assert recorder.bytes_per_buffer == 32000
        self._prepared(recorder, [b"\x00\x00" * 16000])
        recorder.drain_ready(requeue=False)
        assert recorder.seconds_recorded == pytest.approx(1.0)

    def test_all_zero_audio_is_kept_not_dropped(self):
        """Silence is legitimate audio. The old bug made it indistinguishable."""
        recorder = Recorder()
        silence = b"\x00" * 64
        self._prepared(recorder, [silence])
        assert recorder.drain_ready(requeue=False) == 64
        assert recorder.read_available() == silence


@pytest.mark.slow
class TestLiveCapture:
    """Needs a working microphone. Asserts the shape of real room tone."""

    def test_captured_audio_is_not_full_scale_garbage(self):
        import time

        recorder = Recorder(sample_rate=16000)
        recorder.start()
        time.sleep(1.0)
        data = recorder.read_available()
        recorder.stop()

        assert len(data) > 16000, "expected roughly a second of audio"
        samples = struct.unpack(f"<{len(data) // 2}h", data)
        peak = max(abs(v) for v in samples)
        mean = sum(abs(v) for v in samples) / len(samples)

        # Heap garbage read as int16 sits near full scale with a huge mean.
        # Any real room, quiet or not, is far below that.
        assert peak < 32000, f"peak {peak} looks like uninitialised memory"
        assert mean < 8000, f"mean |x| {mean:.0f} looks like uninitialised memory"

    def test_duration_matches_wall_clock(self):
        import time

        recorder = Recorder(sample_rate=16000)
        recorder.start()
        time.sleep(1.0)
        recorder.read_available()
        recorder.stop()
        assert 0.7 < recorder.seconds_recorded < 1.4
