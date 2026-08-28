"""Chunk boundary decisions.

Whisper's encoder consumes exactly 30 s as one independent window, and with
condition_on_previous_text=False nothing links one window to the next. So audio
behind a closed boundary will never be improved by more context, and can be
finalised immediately while the user is still speaking.

Boundaries are placed at silence gaps so they land where sentences end -- see
"seam artifacts" in PLAN.md for why the gap threshold is the most consequential
tunable in the whole system.

Pure logic over VAD output: no audio, no model, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import NamedTuple


class Speech(NamedTuple):
    """A detected speech region, in seconds relative to the buffer start."""

    start: float
    end: float


# Boundary times are floats: VAD returns seconds, and buffer duration is derived
# from a sample count. Comparing a difference of two of them against a threshold
# loses a bit or two (2.8 - 2.0 == 0.7999999999999998), which without slack turns
# into a silently missed boundary.
_EPS = 1e-6


class Action(Enum):
    CONTINUE = auto()  # keep accumulating
    CLOSE = auto()  # close a chunk at cut_at and transcribe it
    DROP = auto()  # no speech at all: discard without transcribing


@dataclass(frozen=True)
class Decision:
    action: Action
    cut_at: float = 0.0
    forced: bool = False  # True when cut by max_chunk_s rather than by silence

    @property
    def should_transcribe(self) -> bool:
        return self.action is Action.CLOSE and self.cut_at > 0.0


def decide(
    speech: list[Speech],
    buffer_s: float,
    silence_gap_s: float,
    max_chunk_s: float,
    ended: bool = False,
) -> Decision:
    """Decide what to do with the audio accumulated so far.

    ``ended`` is True when the hotkey has been released, so whatever remains
    must be closed out rather than waiting for a silence gap that will never
    arrive.
    """
    if not speech:
        # Nothing but silence. Never hand this to the model: fed pure silence,
        # Whisper reliably emits training-set residue such as " Thank you."
        # (verified on both large-v3 and turbo on this machine).
        if ended or buffer_s + _EPS >= max_chunk_s:
            return Decision(Action.DROP)
        return Decision(Action.CONTINUE)

    # An internal gap that is already in the buffer is just as valid a boundary
    # as a trailing one, and closing at the earliest complete phrase keeps
    # chunks small. This matters whenever audio arrives faster than it is
    # processed -- most often when the GPU is busy with a rolling final and the
    # buffer grows straight through a pause.
    for earlier, later in zip(speech, speech[1:]):
        if later.start - earlier.end + _EPS >= silence_gap_s:
            return Decision(Action.CLOSE, cut_at=earlier.end)

    last_end = speech[-1].end
    trailing_silence = buffer_s - last_end

    if trailing_silence + _EPS >= silence_gap_s:
        # A real pause: the natural place to cut, and the model has slack now.
        return Decision(Action.CLOSE, cut_at=last_end)

    if buffer_s + _EPS >= max_chunk_s:
        # Talked straight through the window. Cutting here can split a word;
        # rare, and it costs one word rather than the whole dictation.
        return Decision(Action.CLOSE, cut_at=max_chunk_s, forced=True)

    if ended:
        # Released mid-phrase: close at the last speech, trimming the trailing
        # silence so the model never sees it.
        return Decision(Action.CLOSE, cut_at=last_end)

    return Decision(Action.CONTINUE)


def total_speech_duration(speech: list[Speech]) -> float:
    return sum(max(0.0, s.end - s.start) for s in speech)
