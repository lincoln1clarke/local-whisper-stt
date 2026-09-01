"""End-to-end worker tests: spawn the real subprocess and talk to it.

This exercises everything the supervisor depends on -- process spawn, the pipe
protocol, chunk boundary detection, both transcription passes, the silence
guard and logging -- without a keyboard or a microphone.
"""

from __future__ import annotations

import json
import threading
import time
import wave
from pathlib import Path

import pytest

from lwstt.core.config import ModelsSection, expand_path
from lwstt.core.protocol import Msg
from lwstt.worker_client import WorkerClient

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
# Same source of truth as the app, so a machine that runs the app runs the
# tests, and neither file names a user account.
MODEL_DIR = expand_path(ModelsSection.dir)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (MODEL_DIR / "faster-whisper-large-v3").is_dir(),
        reason="models not installed",
    ),
]


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        return handle.readframes(handle.getnframes())


class Harness:
    """Drives a worker subprocess and records everything it sends back."""

    def __init__(self, tmp_path: Path, **overrides):
        self.messages: list[tuple[Msg, dict]] = []
        self.done = threading.Event()
        self.ready = threading.Event()
        self.tmp_path = tmp_path
        self.client = WorkerClient(on_message=self._on_message, log=lambda m: None)
        self.settings = {
            "sample_rate": 16000,
            "language": "en",
            "models": {
                "preview": str(MODEL_DIR / "faster-whisper-large-v3-turbo"),
                "final": str(MODEL_DIR / "faster-whisper-large-v3"),
                "device": "cuda",
                "compute_type": "float16",
            },
            "chunking": {"silence_gap_ms": 800, "max_chunk_s": 25.0},
            "preview": {"enabled": False, "refresh_ms": 400, "beam_size": 1},
            "final": {
                "beam_size": 5,
                "condition_on_previous_text": False,
                "vad_filter": True,
                "language": "en",
            },
            "vocabulary": {"file": "", "max_tokens": 224, "mode": "hotwords"},
            "filler": {"file": "", "enabled": False},
            "logging": {
                "enabled": True,
                "dir": str(tmp_path / "logs"),
                "format": "wav",
                "bitrate_kbps": 24,
            },
        }
        self.settings.update(overrides)

    def _on_message(self, msg, obj):
        self.messages.append((msg, obj))
        if msg is Msg.READY:
            self.ready.set()
        if msg is Msg.DONE:
            self.done.set()

    def __enter__(self):
        self.client.start()
        return self

    def __exit__(self, *exc):
        self.client.stop()

    def dictate(self, pcm: bytes, timeout: float = 180.0, feed_chunk: int = 3200):
        """Stream audio the way the supervisor does, then release."""
        self.client.start_dictation(self.settings)
        assert self.ready.wait(120), "worker never reported READY"
        for i in range(0, len(pcm), feed_chunk):
            self.client.send_audio(pcm[i : i + feed_chunk])
            time.sleep(0.005)
        self.client.end_dictation()
        assert self.done.wait(timeout), "worker never reported DONE"
        return self.final_text()

    def commits(self) -> list[str]:
        return [o.get("text", "") for m, o in self.messages if m is Msg.COMMIT]

    def final_text(self) -> str:
        for msg, obj in reversed(self.messages):
            if msg is Msg.DONE:
                return obj.get("text", "")
        return ""

    def errors(self) -> list[str]:
        return [o.get("message", "") for m, o in self.messages if m is Msg.ERROR]


class TestRoundTrip:
    def test_simple_sentence_end_to_end(self, tmp_path):
        with Harness(tmp_path) as h:
            text = h.dictate(read_pcm(FIXTURES / "simple.wav"))
        assert "quick brown fox" in text.lower()
        assert h.errors() == []

    def test_two_part_audio_commits_more_than_once(self, tmp_path):
        """Rolling finalization: the pause should close a chunk mid-dictation."""
        with Harness(tmp_path) as h:
            text = h.dictate(read_pcm(FIXTURES / "two_part.wav"))
        committed = [c for c in h.commits() if c]
        assert len(committed) >= 2, f"expected a mid-dictation commit, got {h.commits()}"
        lowered = text.lower()
        assert "first sentence" in lowered
        assert "second sentence" in lowered

    def test_committed_chunks_are_space_joined(self, tmp_path):
        with Harness(tmp_path) as h:
            text = h.dictate(read_pcm(FIXTURES / "two_part.wav"))
        assert "  " not in text
        assert text == text.strip()

    def test_worker_survives_a_second_dictation(self, tmp_path):
        with Harness(tmp_path) as h:
            first = h.dictate(read_pcm(FIXTURES / "simple.wav"))
            h.done.clear()
            second = h.dictate(read_pcm(FIXTURES / "numbers.wav"))
        assert "fox" in first.lower()
        assert "widgets" in second.lower()


class TestSilence:
    def test_silence_produces_no_text(self, tmp_path):
        """The guard, exercised through the full pipeline."""
        silence = b"\x00\x00" * 16000 * 3
        with Harness(tmp_path) as h:
            text = h.dictate(silence)
        assert text == "", f"silence produced {text!r}"
        assert all(not c for c in h.commits())

    def test_silence_then_speech_still_transcribes(self, tmp_path):
        pcm = b"\x00\x00" * 16000 * 2 + read_pcm(FIXTURES / "simple.wav")
        with Harness(tmp_path) as h:
            text = h.dictate(pcm)
        assert "fox" in text.lower()


class TestFillerAndVocabulary:
    def test_filler_list_is_applied(self, tmp_path):
        filler = tmp_path / "filler.md"
        filler.write_text("- quick\n", encoding="utf-8")
        with Harness(tmp_path) as h:
            h.settings["filler"] = {"file": str(filler), "enabled": True}
            text = h.dictate(read_pcm(FIXTURES / "simple.wav"))
        assert "quick" not in text.lower()
        assert "brown fox" in text.lower()

    def test_vocabulary_file_is_accepted(self, tmp_path):
        vocab = tmp_path / "vocabulary.md"
        vocab.write_text("- Hopf\n- Grothendieck\n", encoding="utf-8")
        with Harness(tmp_path) as h:
            h.settings["vocabulary"] = {
                "file": str(vocab), "max_tokens": 224, "mode": "hotwords"
            }
            text = h.dictate(read_pcm(FIXTURES / "simple.wav"))
        assert "fox" in text.lower()
        assert h.errors() == []


def wait_for_log(log_dir, pattern="*.json", timeout=60):
    """Wait for the dictation log to land.

    Logging moved off the reply thread so DONE is not held up by the WAV write
    and the Opus encode, which cost roughly a fifth of a second per minute of
    speech. That makes the log asynchronous: DONE arriving no longer means the
    files exist yet.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log_dir.exists():
            hits = [f for day in log_dir.iterdir() for f in day.glob(pattern)]
            if hits:
                return hits
        time.sleep(0.05)
    raise AssertionError(f"no {pattern} under {log_dir} within {timeout}s")


class TestLogging:
    def test_a_dictation_is_logged_with_audio_and_metadata(self, tmp_path):
        with Harness(tmp_path) as h:
            h.dictate(read_pcm(FIXTURES / "simple.wav"))
            metas = wait_for_log(tmp_path / "logs")
        days = list((tmp_path / "logs").iterdir())
        assert len(days) == 1
        assert len(metas) == 1
        payload = json.loads(metas[0].read_text(encoding="utf-8"))
        assert "quick brown fox" in payload["text"].lower()
        assert payload["duration_s"] > 1
        assert payload["segments"], "segment timestamps are required for training use"
        assert payload["segments"][0]["end"] > payload["segments"][0]["start"]

    def test_logging_can_be_disabled(self, tmp_path):
        with Harness(tmp_path) as h:
            h.settings["logging"] = {"enabled": False, "dir": str(tmp_path / "logs")}
            h.dictate(read_pcm(FIXTURES / "simple.wav"))
        time.sleep(1.0)  # give an errant background write time to appear
        assert not (tmp_path / "logs").exists()

    def test_silence_is_still_logged_but_has_no_text(self, tmp_path):
        with Harness(tmp_path) as h:
            h.dictate(b"\x00\x00" * 16000 * 3)
        days = list((tmp_path / "logs").iterdir())
        payload = json.loads(next(days[0].glob("*.json")).read_text(encoding="utf-8"))
        assert payload["text"] == ""


class TestLifecycle:
    def test_abort_discards_without_committing(self, tmp_path):
        with Harness(tmp_path) as h:
            h.client.start_dictation(h.settings)
            assert h.ready.wait(120)
            h.client.send_audio(read_pcm(FIXTURES / "simple.wav"))
            h.client.abort()
            time.sleep(1.0)
            assert not h.done.is_set()
            assert all(not c for c in h.commits())

    def test_ping_reports_state(self, tmp_path):
        with Harness(tmp_path) as h:
            h.client.ping()
            deadline = time.time() + 10
            while time.time() < deadline:
                if any(m is Msg.PONG for m, _ in h.messages):
                    break
                time.sleep(0.05)
            pongs = [o for m, o in h.messages if m is Msg.PONG]
        assert pongs, "worker did not answer PING"
        assert "dictating" in pongs[0]

    def test_stop_terminates_the_process(self, tmp_path):
        h = Harness(tmp_path)
        h.client.start()
        assert h.client.alive
        h.client.stop()
        assert not h.client.alive

    def test_sending_to_a_dead_worker_fails_gracefully(self, tmp_path):
        h = Harness(tmp_path)
        h.client.start()
        h.client.stop()
        assert h.client.send_audio(b"\x00\x00") is False
        assert h.client.end_dictation() is False


class TestPreviewLoop:
    """The preview path, which the other tests deliberately disable."""

    def test_preview_messages_arrive_during_a_dictation(self, tmp_path):
        with Harness(tmp_path) as h:
            h.settings["preview"] = {"enabled": True, "refresh_ms": 200, "beam_size": 1}
            # Feed at roughly real time so the preview loop has something to chase.
            h.client.start_dictation(h.settings)
            assert h.ready.wait(120)
            pcm = read_pcm(FIXTURES / "simple.wav")
            step = 16000 * 2 // 10  # 100 ms
            for i in range(0, len(pcm), step):
                h.client.send_audio(pcm[i : i + step])
                time.sleep(0.1)
            h.client.end_dictation()
            assert h.done.wait(180)
            previews = [o.get("text", "") for m, o in h.messages if m is Msg.PREVIEW]
        assert previews, "no preview was ever emitted"
        assert any("fox" in p.lower() or "quick" in p.lower() for p in previews)

    def test_preview_is_superseded_by_the_commit(self, tmp_path):
        with Harness(tmp_path) as h:
            h.settings["preview"] = {"enabled": True, "refresh_ms": 200, "beam_size": 1}
            h.client.start_dictation(h.settings)
            assert h.ready.wait(120)
            pcm = read_pcm(FIXTURES / "simple.wav")
            step = 16000 * 2 // 10
            for i in range(0, len(pcm), step):
                h.client.send_audio(pcm[i : i + step])
                time.sleep(0.1)
            h.client.end_dictation()
            assert h.done.wait(180)
            order = [m for m, _ in h.messages if m in (Msg.PREVIEW, Msg.COMMIT)]
        assert Msg.COMMIT in order
        assert order[-1] is Msg.COMMIT, "a preview must never be the last word"
        assert "quick brown fox" in h.final_text().lower()

    def test_silence_produces_no_preview(self, tmp_path):
        with Harness(tmp_path) as h:
            h.settings["preview"] = {"enabled": True, "refresh_ms": 200, "beam_size": 1}
            h.client.start_dictation(h.settings)
            assert h.ready.wait(120)
            for _ in range(15):
                h.client.send_audio(b"\x00\x00" * 1600)
                time.sleep(0.1)
            h.client.end_dictation()
            assert h.done.wait(120)
            previews = [o.get("text", "") for m, o in h.messages if m is Msg.PREVIEW]
        assert previews == [], f"silence produced previews: {previews}"
