"""Model management and transcription.

Two model slots, loaded lazily and evictable independently:

  * preview -- turbo, greedy, run every ~400 ms on the open chunk
  * final   -- large-v3, beam search, run once per closed chunk

Both live here in the worker; the supervisor never imports any of it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from ..core.chunker import Speech

# Populated on first use so importing this module stays cheap.
_WhisperModel = None
_get_speech_timestamps = None
_VadOptions = None


def _import_backend() -> None:
    global _WhisperModel, _get_speech_timestamps, _VadOptions
    if _WhisperModel is not None:
        return
    from . import cuda

    cuda.prepare()
    from faster_whisper import WhisperModel
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    _WhisperModel = WhisperModel
    _get_speech_timestamps = get_speech_timestamps
    _VadOptions = VadOptions


def pcm_to_float(pcm: bytes) -> np.ndarray:
    """int16 little-endian bytes -> float32 in [-1, 1], as Whisper expects."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    samples = np.frombuffer(pcm, dtype="<i2")
    return (samples.astype(np.float32) / 32768.0).copy()


@dataclass
class Segment:
    start: float
    end: float
    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0


class ModelSlot:
    """One lazily loaded model that can be dropped to reclaim VRAM."""

    def __init__(self, path: str, device: str, compute_type: str) -> None:
        self.path = path
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._lock = threading.Lock()
        self.last_used = 0.0
        self.load_seconds = 0.0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def get(self):
        with self._lock:
            if self._model is None:
                _import_backend()
                started = time.monotonic()
                self._model = _WhisperModel(
                    self.path, device=self.device, compute_type=self.compute_type
                )
                self.load_seconds = time.monotonic() - started
            self.last_used = time.monotonic()
            return self._model

    def unload(self) -> None:
        with self._lock:
            self._model = None


def speech_segments(
    audio: np.ndarray,
    sample_rate: int = 16000,
    min_silence_ms: int = 200,
    threshold: float = 0.5,
    speech_pad_ms: int = 0,
) -> list[Speech]:
    """Run Silero VAD and return speech regions in seconds.

    This is the guard that keeps silence away from the model. Fed pure silence,
    Whisper reliably emits training-set residue -- verified on this machine,
    both large-v3 and turbo return " Thank you." for three seconds of digital
    silence with VAD disabled.

    ``speech_pad_ms`` defaults to **0**, not Silero's 400. Padding widens every
    speech region on both sides, which narrows every gap between them by twice
    the padding: a real 2.43 s pause measures as 1.63 s at the default. Chunk
    boundaries are decided from these gaps, so padding silently doubles the
    pause a speaker must leave before a chunk will close -- and a chunk that
    never closes grows until the forced cut, dragging the preview pass with it.
    Boundaries are padded explicitly at the cut instead (see CUT_PAD_S).
    """
    if audio.size == 0:
        return []
    _import_backend()
    options = _VadOptions(
        threshold=threshold,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=speech_pad_ms,
    )
    stamps = _get_speech_timestamps(audio, options, sampling_rate=sample_rate)
    return [Speech(s["start"] / sample_rate, s["end"] / sample_rate) for s in stamps]


def transcribe(
    slot: ModelSlot,
    audio: np.ndarray,
    *,
    language: str = "en",
    beam_size: int = 5,
    hotwords: str | None = None,
    initial_prompt: str | None = None,
    condition_on_previous_text: bool = False,
    vad_filter: bool = True,
) -> list[Segment]:
    """Transcribe one chunk of audio.

    ``condition_on_previous_text`` defaults to False: chunks are independent by
    design, and carrying text forward is a known source of drift and repetition
    loops.
    """
    if audio.size == 0:
        return []
    model = slot.get()
    segments, _info = model.transcribe(
        audio,
        language=language,
        beam_size=beam_size,
        condition_on_previous_text=condition_on_previous_text,
        vad_filter=vad_filter,
        hotwords=hotwords or None,
        initial_prompt=initial_prompt or None,
        word_timestamps=False,
    )
    return [
        Segment(
            start=s.start,
            end=s.end,
            text=s.text,
            avg_logprob=getattr(s, "avg_logprob", 0.0),
            no_speech_prob=getattr(s, "no_speech_prob", 0.0),
        )
        for s in segments
    ]


def segments_text(segments: list[Segment]) -> str:
    return "".join(s.text for s in segments).strip()


def count_tokens_with(slot: ModelSlot):
    """A token counter backed by the model's real tokenizer.

    Injected into core.wordlists.fit_to_budget so the ~224-token prompt budget
    is measured rather than estimated, without core depending on any of this.
    """

    def count(text: str) -> int:
        model = slot.get()
        try:
            return len(model.hf_tokenizer.encode(text).ids)
        except Exception:
            return max(1, (len(text) + 3) // 4)

    return count
