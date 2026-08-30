"""Recovery paths: the dictation must always end, and never lose its tail.

These cover three failures seen in real use:

  * the last word spoken as the keys were released never appeared
  * tildes left stranded on screen with no explanation
  * a dictation that produced nothing at all, for minutes, then recovered
"""

from __future__ import annotations

import time

from lwstt.core.protocol import Msg

from conftest import FakeRecorder


class TailRecorder(FakeRecorder):
    """A recorder whose final buffer only becomes readable once stopped.

    Mirrors winmm: waveInReset hands back the partially filled buffer the driver
    was still writing into. Reading before stopping loses it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tail = b"\xaa\xbb" * 400

    def stop(self):
        self.stopped = True
        self.pending += self.tail


def _run_to_threshold(sup):
    sup._arm()
    sup.armed_at -= 10  # pretend the keys have been held long enough
    sup._pump_once()


class TestAudioTailOnRelease:
    def test_the_final_buffer_is_not_lost(self, sup, monkeypatch):
        """The word being spoken as the keys are released must reach the worker."""
        import lwstt.supervisor as module

        monkeypatch.setattr(module, "Recorder", TailRecorder)
        _run_to_threshold(sup)
        sup._disarm()
        assert b"\xaa\xbb" in bytes(sup.worker.audio), "trailing audio was dropped"

    def test_the_tail_is_sent_before_end(self, sup, monkeypatch):
        import lwstt.supervisor as module

        order = []
        monkeypatch.setattr(module, "Recorder", TailRecorder)
        _run_to_threshold(sup)
        sup.worker.send_audio = lambda p: (order.append("audio"), True)[1]
        sup.worker.end_dictation = lambda: (order.append("end"), True)[1]
        sup._disarm()
        assert order == ["audio", "end"]

    def test_the_recorder_is_stopped_before_it_is_read(self, sup, monkeypatch):
        import lwstt.supervisor as module

        events = []

        class Watcher(FakeRecorder):
            def stop(self):
                events.append("stop")
                super().stop()

            def read_available(self):
                events.append("read")
                return super().read_available()

        monkeypatch.setattr(module, "Recorder", Watcher)
        _run_to_threshold(sup)
        events.clear()
        sup._disarm()
        assert events.index("stop") < events.index("read")


class TestFinalizeWatchdog:
    """A dead or wedged worker must never leave the user staring at tildes."""

    def test_release_starts_the_clock(self, sup):
        _run_to_threshold(sup)
        sup._disarm()
        assert sup._awaiting_done_since is not None

    def test_done_stops_the_clock(self, sup):
        _run_to_threshold(sup)
        sup._disarm()
        sup._on_worker_message(Msg.DONE, {"session": sup.session, "text": "ok"})
        assert sup._awaiting_done_since is None

    def test_timeout_clears_the_markers(self, sup):
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "stranded"})
        sup._disarm()
        sup._awaiting_done_since = time.monotonic() - 999
        sup.typed.clear()
        sup._check_finalize_timeout()
        assert sup.typing.on_screen() == ""
        assert sup.typed != [], "should have backspaced the stray tildes away"
        assert sup._awaiting_done_since is None

    def test_committed_text_survives_the_timeout(self, sup):
        """Only the provisional tail is removed; finalised text is kept."""
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.COMMIT, {"session": sup.session, "text": "Kept."})
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "dropped"})
        sup._disarm()
        sup._awaiting_done_since = time.monotonic() - 999
        sup._check_finalize_timeout()
        assert not sup.dictating

    def test_no_timeout_before_the_deadline(self, sup):
        _run_to_threshold(sup)
        sup._disarm()
        sup.typed.clear()
        sup._check_finalize_timeout()
        assert sup.typed == []

    def test_idle_supervisor_is_unaffected(self, sup):
        sup._check_finalize_timeout()
        assert sup.typed == []

    def test_a_dead_worker_on_release_finishes_immediately(self, sup):
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "stranded"})
        sup.worker.end_dictation = lambda: False  # worker unreachable
        sup._disarm()
        assert sup.typing.on_screen() == ""
        assert not sup.dictating

    def test_worker_death_mid_dictation_clears_the_markers(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "stranded"})
        sup._on_worker_exit()
        assert sup.typing.on_screen() == ""
        assert not sup.dictating

    def test_a_new_dictation_starts_clean_after_a_timeout(self, sup):
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "stranded"})
        sup._disarm()
        sup._awaiting_done_since = time.monotonic() - 999
        sup._check_finalize_timeout()
        _run_to_threshold(sup)
        assert sup.typing.committed == ""
        assert sup.typing.provisional == ""
        assert sup.dictating


class TestStrandedByFocusChange:
    """Markers left behind when focus moves mid-dictation.

    Typing into the new window would delete text in a document these characters
    never went into, so the cleanup waits for focus to come back rather than
    forgetting the markers exist.
    """

    def _strand(self, sup, module, monkeypatch):
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello"})
        monkeypatch.setattr(module, "foreground_window", lambda: 99999)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello there"})

    def test_nothing_is_typed_into_the_new_window(self, sup, monkeypatch):
        import lwstt.supervisor as module

        self._strand(sup, module, monkeypatch)
        sup.typed.clear()
        sup._try_deferred_cleanup()
        assert sup.typed == [], "must not backspace into someone else's document"

    def test_the_stranded_markers_are_remembered(self, sup, monkeypatch):
        import lwstt.supervisor as module

        self._strand(sup, module, monkeypatch)
        assert sup._cleanup_hwnd == 12345
        assert sup.typing.on_screen() != "", "state must survive to describe the cleanup"

    def test_cleanup_runs_when_focus_returns(self, sup, monkeypatch):
        import lwstt.supervisor as module

        self._strand(sup, module, monkeypatch)
        sup.typed.clear()
        monkeypatch.setattr(module, "foreground_window", lambda: 12345)
        sup._try_deferred_cleanup()
        assert sup.typed != [], "markers should be removed once focus is back"
        assert sup.typing.on_screen() == ""
        assert sup._cleanup_hwnd == 0

    def test_cleanup_gives_up_after_the_grace_period(self, sup, monkeypatch):
        import lwstt.supervisor as module

        self._strand(sup, module, monkeypatch)
        sup._cleanup_deadline = time.monotonic() - 1
        sup.typed.clear()
        sup._try_deferred_cleanup()
        assert sup.typed == []
        assert sup._cleanup_hwnd == 0, "must not retry forever"

    def test_a_new_dictation_supersedes_a_pending_cleanup(self, sup, monkeypatch):
        import lwstt.supervisor as module

        self._strand(sup, module, monkeypatch)
        monkeypatch.setattr(module, "foreground_window", lambda: 12345)
        _run_to_threshold(sup)
        assert sup._cleanup_hwnd == 0
        assert sup.typing.committed == ""

    def test_idle_supervisor_does_nothing(self, sup):
        sup._try_deferred_cleanup()
        assert sup.typed == []

    def test_focus_change_is_logged_once_per_press(self, sup, monkeypatch):
        import lwstt.supervisor as module

        messages = []
        sup.log = messages.append
        self._strand(sup, module, monkeypatch)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "more"})
        # "aborted: focus changed" also contains the phrase; count the
        # diagnostic line itself, which is the noisy one.
        assert sum(m.startswith("focus changed") for m in messages) == 1
