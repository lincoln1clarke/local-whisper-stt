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

    def test_the_dictation_is_not_aborted(self, sup, monkeypatch):
        import lwstt.supervisor as module

        self._strand(sup, module, monkeypatch)
        assert sup.dictating, "a focus change must not end a dictation in progress"

    def test_the_stranded_markers_are_remembered(self, sup, monkeypatch):
        import lwstt.supervisor as module

        self._strand(sup, module, monkeypatch)
        assert sup._cleanup_hwnd == 12345
        assert sup.typing.on_screen() != "", "state must survive to describe the cleanup"

    def test_focus_returning_mid_dictation_just_resumes(self, sup, monkeypatch):
        import lwstt.supervisor as module

        self._strand(sup, module, monkeypatch)
        sup.typed.clear()
        monkeypatch.setattr(module, "foreground_window", lambda: 12345)
        sup._try_deferred_cleanup()
        assert sup._cleanup_hwnd == 0
        assert sup.typing.on_screen() != "", "still speaking: nothing to clean up"
        assert sup.typed == []

    def test_cleanup_runs_when_focus_returns_after_the_dictation_ended(
        self, sup, monkeypatch
    ):
        import lwstt.supervisor as module

        self._strand(sup, module, monkeypatch)
        sup.dictating = False  # keys released while focus was away
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
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "more still"})
        assert sum(m.startswith("focus moved") for m in messages) == 1


class TestDictationAlwaysEndsClean:
    """The screen must be empty of markers however a dictation ends.

    The specific hole: set_listening(False) only removes the *empty* marker
    pair. While provisional text is showing, the wrap comes from the text rather
    than the listening flag, so the call is a no-op and the preview stays on
    screen with its tildes. It happens whenever the worker's final drain finds
    only silence -- no commit is sent, so nothing supersedes the last preview.
    """

    def test_done_clears_a_preview_that_was_never_committed(self, sup):
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "never committed"})
        assert sup.typing.on_screen() != ""
        sup._on_worker_message(Msg.DONE, {"session": sup.session, "text": ""})
        assert sup.typing.on_screen() == "", "the preview and its tildes must go"

    def test_done_clears_the_bare_listening_markers(self, sup):
        _run_to_threshold(sup)
        assert "~~" in sup.typing.on_screen()
        sup._on_worker_message(Msg.DONE, {"session": sup.session, "text": ""})
        assert sup.typing.on_screen() == ""

    def test_done_keeps_committed_text(self, sup):
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.COMMIT, {"session": sup.session, "text": "Kept."})
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "dropped"})
        sup._on_worker_message(Msg.DONE, {"session": sup.session, "text": "Kept."})
        assert sup.typing.on_screen() == ""
        assert not sup.dictating

    def test_cancel_clears_a_preview(self, sup):
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "half a thought"})
        sup.cancel_pending("user typed")
        assert sup.typing.on_screen() == ""

    def test_finish_locally_clears_a_preview(self, sup):
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "half a thought"})
        sup._finish_locally("worker exited")
        assert sup.typing.on_screen() == ""

    def test_leftovers_are_logged_and_scheduled_for_cleanup(self, sup, monkeypatch):
        """If the invariant ever fails, say so rather than leaving it on screen."""
        import lwstt.supervisor as module

        messages = []
        sup.log = messages.append
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "stuck"})
        monkeypatch.setattr(module, "foreground_window", lambda: 99999)
        sup._on_worker_message(Msg.DONE, {"session": sup.session, "text": ""})
        assert any("still on screen" in m for m in messages)
        assert sup._cleanup_hwnd == 12345, "and it must be queued for cleanup"

    def test_every_ending_leaves_dictating_false(self, sup):
        for ending in ("done", "cancel", "local"):
            _run_to_threshold(sup)
            sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "x"})
            if ending == "done":
                sup._on_worker_message(Msg.DONE, {"session": sup.session, "text": ""})
            elif ending == "cancel":
                sup.cancel_pending("user typed")
            else:
                sup._finish_locally("worker exited")
            assert not sup.dictating, ending
            assert sup.typing.on_screen() == "", ending


class TestMarkersDoNotOutliveThePress:
    """The empty "~~" pair must vanish when the keys come up.

    It means "listening". Once the keys are released nothing more is being
    listened to and no further preview can arrive, so waiting for DONE to
    remove it makes it hang around for however long the tail takes -- which is
    exactly the stranded-tilde complaint. It goes at release instead, so no
    delay downstream can strand it.
    """

    def test_release_clears_the_empty_pair(self, sup, monkeypatch):
        import lwstt.supervisor as module

        monkeypatch.setattr(module, "Recorder", TailRecorder)
        _run_to_threshold(sup)
        assert sup.typing.wrapped_provisional() == "~~", "markers never appeared"
        sup._disarm()
        assert sup.typing.wrapped_provisional() == "", "markers outlived the press"

    def test_release_keeps_provisional_text(self, sup, monkeypatch):
        """A preview still on screen is replaced by its commit, not deleted."""
        import lwstt.supervisor as module

        monkeypatch.setattr(module, "Recorder", TailRecorder)
        _run_to_threshold(sup)
        sup._on_worker_message(Msg.PREVIEW, {"text": "half a thought", "session": sup.session})
        sup._disarm()
        assert sup.typing.provisional == "half a thought"

    def test_a_late_done_still_ends_the_dictation(self, sup, monkeypatch):
        """Clearing early must not leave the dictation half-open."""
        import lwstt.supervisor as module

        monkeypatch.setattr(module, "Recorder", TailRecorder)
        _run_to_threshold(sup)
        sup._disarm()
        assert sup.dictating, "still waiting on the worker"
        sup._on_worker_message(Msg.DONE, {"text": "hello", "session": sup.session})
        assert not sup.dictating
        assert sup.typing.wrapped_provisional() == ""


class TestStaleDoneIsNotSilentlyDropped:
    def test_a_stale_done_still_ends_the_dictation(self, sup, monkeypatch):
        """Ignoring it outright left the dictation looking permanently unfinished."""
        import lwstt.supervisor as module

        monkeypatch.setattr(module, "Recorder", TailRecorder)
        lines: list[str] = []
        sup.log = lines.append
        _run_to_threshold(sup)
        sup._disarm()
        sup._on_worker_message(Msg.DONE, {"text": "x", "session": sup.session - 1})
        assert not sup.dictating
        assert any("stale DONE" in line for line in lines)
