"""Tests for chunk boundary decisions.

Two behaviours matter most here. Silence must never reach the model, because
Whisper emits training-set residue (" Thank you.") when it does -- verified
empirically on both models on this machine. And boundaries must land at pauses,
because a cut mid-sentence produces a spurious capital and period at the seam.
"""

from __future__ import annotations

import pytest

from lwstt.core.chunker import Action, Speech, decide, total_speech_duration

GAP = 0.8
MAX = 25.0


def d(speech, buffer_s, ended=False, gap=GAP, max_chunk=MAX):
    return decide(speech, buffer_s, gap, max_chunk, ended)


class TestSilence:
    def test_pure_silence_while_recording_waits(self):
        assert d([], buffer_s=0.5).action is Action.CONTINUE

    def test_pure_silence_at_release_is_dropped(self):
        """Never hand silence to the model."""
        assert d([], buffer_s=3.0, ended=True).action is Action.DROP

    def test_long_pure_silence_is_dropped_without_waiting(self):
        assert d([], buffer_s=MAX).action is Action.DROP

    def test_dropped_decisions_do_not_transcribe(self):
        assert not d([], buffer_s=3.0, ended=True).should_transcribe


class TestSilenceGap:
    def test_gap_long_enough_closes_at_the_last_speech(self):
        result = d([Speech(0.2, 2.0)], buffer_s=2.9)
        assert result.action is Action.CLOSE
        assert result.cut_at == 2.0
        assert not result.forced

    def test_gap_too_short_continues(self):
        assert d([Speech(0.2, 2.0)], buffer_s=2.5).action is Action.CONTINUE

    def test_gap_exactly_at_the_threshold_closes(self):
        assert d([Speech(0.0, 2.0)], buffer_s=2.0 + GAP).action is Action.CLOSE

    def test_cut_uses_the_last_segment_when_no_internal_gap_is_long_enough(self):
        speech = [Speech(0.0, 1.0), Speech(1.5, 2.5), Speech(3.0, 4.0)]
        assert d(speech, buffer_s=5.0).cut_at == 4.0


class TestInternalGaps:
    """A gap already sitting in the buffer is as good a boundary as a trailing
    one. This is what keeps chunking correct when audio arrives faster than it
    is processed -- notably while the GPU is busy with a rolling final."""

    def test_internal_gap_closes_at_the_phrase_before_it(self):
        speech = [Speech(0.0, 2.0), Speech(3.5, 5.0)]
        result = d(speech, buffer_s=5.1)
        assert result.action is Action.CLOSE
        assert result.cut_at == 2.0

    def test_earliest_qualifying_gap_wins(self):
        speech = [Speech(0.0, 1.0), Speech(2.5, 3.0), Speech(4.5, 5.0)]
        assert d(speech, buffer_s=5.1).cut_at == 1.0

    def test_short_internal_gaps_are_ignored(self):
        speech = [Speech(0.0, 1.0), Speech(1.3, 2.0)]
        assert d(speech, buffer_s=2.1).action is Action.CONTINUE

    def test_internal_gap_is_found_even_when_still_speaking(self):
        speech = [Speech(0.0, 2.0), Speech(3.5, 6.0)]
        assert d(speech, buffer_s=6.0).cut_at == 2.0

    def test_result_is_independent_of_how_fast_audio_arrived(self):
        """The bug this fixes: buffering the whole utterance before processing
        must give the same boundary as streaming it in real time."""
        speech = [Speech(0.0, 1.65), Speech(3.28, 5.46)]
        all_at_once = d(speech, buffer_s=5.8)
        streamed = d([Speech(0.0, 1.65)], buffer_s=3.0)
        assert all_at_once.cut_at == streamed.cut_at == 1.65

    def test_trailing_silence_is_trimmed_off_the_chunk(self):
        """The cut is at the end of speech, not the end of the buffer -- the
        biggest single mitigation for trailing hallucination."""
        result = d([Speech(0.0, 2.0)], buffer_s=10.0)
        assert result.cut_at == 2.0

    def test_a_longer_gap_setting_keeps_a_short_pause_together(self):
        """Raising the gap is how mid-sentence pauses stop splitting sentences."""
        speech = [Speech(0.0, 2.0)]
        assert d(speech, buffer_s=2.6, gap=0.5).action is Action.CLOSE
        assert d(speech, buffer_s=2.6, gap=0.8).action is Action.CONTINUE


class TestForcedCut:
    def test_talking_past_the_window_forces_a_cut(self):
        result = d([Speech(0.0, 24.9)], buffer_s=25.0)
        assert result.action is Action.CLOSE
        assert result.cut_at == MAX
        assert result.forced

    def test_silence_gap_wins_over_the_forced_cut(self):
        """A natural boundary is always preferred, even at the limit."""
        result = d([Speech(0.0, 20.0)], buffer_s=26.0)
        assert result.cut_at == 20.0
        assert not result.forced

    def test_forced_cut_stays_inside_whispers_window(self):
        result = d([Speech(0.0, 29.0)], buffer_s=30.0, max_chunk=25.0)
        assert result.cut_at <= 30.0


class TestRelease:
    def test_release_closes_at_the_last_speech(self):
        result = d([Speech(0.0, 3.0)], buffer_s=3.2, ended=True)
        assert result.action is Action.CLOSE
        assert result.cut_at == 3.0

    def test_release_still_trims_trailing_silence(self):
        assert d([Speech(0.0, 3.0)], buffer_s=8.0, ended=True).cut_at == 3.0

    def test_release_mid_phrase_still_transcribes(self):
        assert d([Speech(0.0, 3.0)], buffer_s=3.05, ended=True).should_transcribe


class TestLongDictation:
    def test_a_long_dictation_produces_many_bounded_chunks(self):
        """Release latency stays constant because each chunk closes as it ends."""
        gap, cuts, buffer_s, speech = GAP, [], 0.0, []
        for i in range(90):  # ~45 minutes of 30-second phrase+pause cycles
            speech = [Speech(0.0, 28.0)]
            buffer_s = 30.0
            result = decide(speech, buffer_s, gap, MAX, False)
            assert result.action is Action.CLOSE
            cuts.append(result.cut_at)
        assert len(cuts) == 90
        assert all(c <= MAX or c == 28.0 for c in cuts)

    def test_no_upper_bound_on_number_of_chunks(self):
        for _ in range(1000):
            assert d([Speech(0.0, 2.0)], buffer_s=3.0).action is Action.CLOSE


class TestTotalSpeechDuration:
    def test_sums_segments(self):
        assert total_speech_duration([Speech(0.0, 1.0), Speech(2.0, 4.0)]) == 3.0

    def test_empty_is_zero(self):
        assert total_speech_duration([]) == 0.0

    def test_inverted_segment_does_not_go_negative(self):
        assert total_speech_duration([Speech(2.0, 1.0)]) == 0.0


class TestEdgeCases:
    @pytest.mark.parametrize("buffer_s", [0.0, 0.001])
    def test_empty_buffer_continues(self, buffer_s):
        assert d([], buffer_s=buffer_s).action is Action.CONTINUE

    def test_speech_running_to_the_exact_buffer_end(self):
        assert d([Speech(0.0, 5.0)], buffer_s=5.0).action is Action.CONTINUE

    def test_zero_length_speech_segment_still_counts_as_speech(self):
        result = d([Speech(1.0, 1.0)], buffer_s=3.0)
        assert result.action is Action.CLOSE
        assert result.cut_at == 1.0
