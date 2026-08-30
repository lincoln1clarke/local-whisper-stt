"""Tests for the worker pipe write path.

A write that fails part-way leaves an incomplete frame in the pipe. Every byte
sent afterwards is read as a continuation of that frame, so the worker soon
decodes raw PCM as a header, finds a zero type byte and dies. Seen in the wild
as "reader stopped: unknown message type 0", which loses the whole dictation
and leaves stray tildes on screen.

PCM is mostly zero bytes, so a stream that slips by even one byte finds a zero
type almost immediately.
"""

from __future__ import annotations

from lwstt.core.protocol import FrameReader, Msg
from lwstt.worker_client import WorkerClient


class FakeStdin:
    def __init__(self, fail_after: int | None = None):
        self.data = bytearray()
        self.writes = 0
        self.fail_after = fail_after

    def write(self, chunk):
        self.writes += 1
        if self.fail_after is not None and self.writes > self.fail_after:
            # Partial write, then failure: the pipe now holds half a frame.
            self.data.extend(chunk[: len(chunk) // 2])
            raise BrokenPipeError("pipe closed")
        self.data.extend(chunk)

    def flush(self):
        pass


class FakeProcess:
    def __init__(self, fail_after=None):
        self.stdin = FakeStdin(fail_after)
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def make_client(process=None):
    client = WorkerClient(on_message=lambda m, o: None, log=lambda m: None)
    if process is not None:
        client._process = process
    return client


class TestFramingStaysIntact:
    def test_messages_round_trip(self):
        process = FakeProcess()
        client = make_client(process)
        assert client.send_audio(b"\x00\x00\x01\x00")
        assert client.end_dictation()
        frames = list(FrameReader().feed(bytes(process.stdin.data)))
        assert frames == [(Msg.AUDIO, b"\x00\x00\x01\x00"), (Msg.END, b"")]

    def test_audio_full_of_nul_bytes_is_framed_correctly(self):
        process = FakeProcess()
        client = make_client(process)
        client.send_audio(b"\x00" * 3200)
        frames = list(FrameReader().feed(bytes(process.stdin.data)))
        assert frames == [(Msg.AUDIO, b"\x00" * 3200)]

    def test_many_frames_stay_aligned(self):
        process = FakeProcess()
        client = make_client(process)
        for i in range(50):
            client.send_audio(bytes([i]) * 320)
        frames = list(FrameReader().feed(bytes(process.stdin.data)))
        assert len(frames) == 50
        assert all(msg is Msg.AUDIO for msg, _ in frames)


class TestPoisonedStream:
    def test_a_failed_write_poisons_the_stream(self):
        client = make_client(FakeProcess(fail_after=0))
        assert client.send_audio(b"\x01\x02") is False
        assert client._stream_broken

    def test_nothing_is_written_after_a_failure(self):
        process = FakeProcess(fail_after=1)
        client = make_client(process)
        client.send_audio(b"\x01" * 100)  # succeeds
        before = len(process.stdin.data)
        client.send_audio(b"\x02" * 100)  # fails part-way, poisons the stream
        after_failure = len(process.stdin.data)
        assert client.send_audio(b"\x03" * 100) is False
        assert client.end_dictation() is False
        assert after_failure > before
        assert len(process.stdin.data) == after_failure, (
            "writing onto a half-written frame is what desynchronises the worker"
        )

    def test_frames_before_the_failure_are_still_readable(self):
        process = FakeProcess(fail_after=1)
        client = make_client(process)
        client.send_audio(b"\x01\x02\x03\x04")
        client.send_audio(b"\x05" * 100)
        frames = list(FrameReader().feed(bytes(process.stdin.data)))
        assert frames[0] == (Msg.AUDIO, b"\x01\x02\x03\x04")

    def test_a_poisoned_stream_forces_a_restart(self):
        process = FakeProcess(fail_after=0)
        client = make_client(process)
        client.send_audio(b"\x01")
        assert client._stream_broken
        started = []
        client.start = lambda: started.append(True)
        client.start_dictation({"session": 1})
        assert process.killed, "the poisoned process must be replaced"
        assert started, "a fresh worker must be started"

    def test_a_restart_clears_the_poison(self):
        client = make_client(FakeProcess(fail_after=0))
        client.send_audio(b"\x01")
        assert client._stream_broken
        client.stop()
        assert not client._stream_broken


class TestDeadProcess:
    def test_sending_with_no_process_is_false_not_an_exception(self):
        client = make_client()
        assert client.send_audio(b"\x01") is False
        assert client.end_dictation() is False
        assert client.abort() is False
        assert client.ping() is False

    def test_empty_audio_is_a_noop(self):
        assert make_client().send_audio(b"") is True

    def test_stop_on_a_dead_client_is_harmless(self):
        client = make_client()
        client.stop()
        assert not client.alive

    def test_alive_is_false_without_a_process(self):
        assert make_client().alive is False
