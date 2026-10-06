"""Supervisor orchestration tests.

The Windows edges are replaced with fakes so the sequencing logic -- arm,
threshold, stream, disarm, abort -- is testable without a keyboard, a
microphone or a GPU. The parts that genuinely need Windows (SendInput, the hook)
are exercised in test_worker_protocol.py and by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lwstt.core.config import Config
from lwstt.core.diff import Edit
from lwstt.core.protocol import Msg

ROOT = Path(__file__).resolve().parent.parent


class FakeRecorder:
    def __init__(self, *args, **kwargs):
        self.started = False
        self.stopped = False
        self.pending = b""
        self.seconds_recorded = 0.0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def read_available(self):
        data, self.pending = self.pending, b""
        return data


class FakeWorker:
    def __init__(self, *args, **kwargs):
        self.alive = True
        self.models_loaded = False
        self.started_with = None
        self.audio = bytearray()
        self.ended = False
        self.aborted = False
        self.stopped = False

    def start(self):
        self.alive = True

    def stop(self, timeout=3.0):
        self.stopped = True
        self.alive = False

    def start_dictation(self, settings):
        self.started_with = settings
        return True

    def send_audio(self, pcm):
        self.audio.extend(pcm)
        return True

    def end_dictation(self):
        self.ended = True
        return True

    def abort(self):
        self.aborted = True
        return True

    def idle_seconds(self):
        return 0.0


@pytest.fixture
def sup(monkeypatch):
    """A Supervisor with every Windows edge replaced."""
    import lwstt.supervisor as module

    typed: list[Edit] = []
    monkeypatch.setattr(module, "Recorder", FakeRecorder)
    monkeypatch.setattr(module, "foreground_window", lambda: 12345)
    monkeypatch.setattr(module, "window_title", lambda h: "fake window")
    monkeypatch.setattr(module, "on_battery", lambda: False)
    monkeypatch.setattr(module, "beep", lambda *a: None)
    monkeypatch.setattr(
        module.sendinput,
        "apply_edit",
        lambda backspaces, text: typed.append(Edit(backspaces, text)),
    )

    class FakeDot:
        def __init__(self):
            self.visible = False
            self.created = False

        def create(self):
            self.created = True

        def show(self):
            self.visible = True

        def hide(self):
            self.visible = False

        def destroy(self):
            self.visible = False

    monkeypatch.setattr(module, "RecordingDot", FakeDot)

    supervisor = module.Supervisor(Config(), ROOT, log=lambda m: None)
    supervisor.worker = FakeWorker()
    supervisor.config.output.min_revision_interval_ms = 0
    supervisor.typed = typed
    return supervisor


class TestArmDisarm:
    def test_arming_opens_the_microphone_and_shows_the_dot(self, sup):
        sup._arm()
        assert sup.recorder.started
        assert sup.dot.visible
        assert sup.target_hwnd == 12345

    def test_arming_does_not_start_a_dictation_yet(self, sup):
        """Nothing is typed before the hold threshold passes."""
        sup._arm()
        assert sup.worker.started_with is None
        assert not sup.dictating

    def test_a_short_press_is_discarded_silently(self, sup):
        sup._arm()
        recorder = sup.recorder
        sup._disarm()
        assert recorder.stopped
        assert sup.recorder is None, "the recorder reference is released on stop"
        assert not sup.worker.ended
        assert sup.typed == [], "a discarded tap must leave nothing behind"

    def test_a_press_past_the_threshold_starts_a_dictation(self, sup):
        sup._arm()
        sup.armed_at -= 10  # pretend the keys have been held for 10 s
        sup._pump_once()
        assert sup.dictating
        assert sup.worker.started_with is not None

    def test_disarm_after_the_threshold_ends_the_dictation(self, sup):
        sup._arm()
        sup.armed_at -= 10
        sup._pump_once()
        sup._disarm()
        assert sup.worker.ended
        assert sup.dot.visible is False

    def test_trailing_audio_is_flushed_on_release(self, sup):
        sup._arm()
        sup.armed_at -= 10
        sup._pump_once()
        sup.recorder.pending = b"\x01\x02" * 100
        sup._disarm()
        assert bytes(sup.worker.audio).endswith(b"\x01\x02")

    def test_microphone_failure_does_not_crash_arming(self, sup, monkeypatch):
        import lwstt.supervisor as module

        def explode(*args, **kwargs):
            raise OSError("device in use")

        monkeypatch.setattr(module, "Recorder", explode)
        sup._arm()
        assert sup.recorder is None
        assert not sup.dictating


class TestTyping:
    def test_preview_is_typed_wrapped_in_markers(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello"})
        assert sup.typing.on_screen() == " ~hello~"

    def test_commit_removes_the_markers(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "helo"})
        sup._on_worker_message(Msg.COMMIT, {"session": sup.session, "text": "Hello."})
        assert sup.typing.on_screen() == " Hello."

    def test_an_empty_commit_clears_the_preview(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "noise"})
        sup._on_worker_message(Msg.COMMIT, {"session": sup.session, "text": ""})
        assert sup.typing.on_screen() == ""

    def test_messages_arriving_after_a_dictation_are_ignored(self, sup):
        """A late reply must not type into whatever now has focus."""
        sup.dictating = False
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "late"})
        assert sup.typed == []

    def test_focus_change_holds_output_without_aborting(self, sup, monkeypatch):
        """Windows hands focus around for momentary reasons -- a tooltip, a
        trackpad tap, another app blinking to the front. Killing a dictation the
        user is still speaking is far worse than pausing its output."""
        import lwstt.supervisor as module

        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello"})
        sup.typed.clear()
        monkeypatch.setattr(module, "foreground_window", lambda: 99999)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello there"})
        assert sup.dictating, "the dictation must survive a focus change"
        assert sup.typed == [], "but nothing may be typed into the other window"

    def test_output_resumes_where_it_left_off(self, sup, monkeypatch):
        """Nothing is mutated while focus is away, so state and screen stay in
        step and typing simply catches up."""
        import lwstt.supervisor as module

        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello"})
        monkeypatch.setattr(module, "foreground_window", lambda: 99999)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello there"})
        monkeypatch.setattr(module, "foreground_window", lambda: 12345)
        sup.typed.clear()
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello there world"})
        assert sup.typed != []
        assert sup.typing.on_screen() == " ~hello there world~"


class TestAbort:
    def test_abort_removes_provisional_text(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello"})
        sup.typed.clear()
        sup.abort(reason="escape")
        assert sup.typed == [Edit(8, "")], "should backspace the space and ~hello~"
        assert not sup.dictating

    def test_abort_keeps_committed_text(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.COMMIT, {"session": sup.session, "text": "Committed."})
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "draft"})
        sup.typed.clear()
        sup.abort(reason="escape")
        assert sup.typing.on_screen() == ""
        # The separator space between committed text and the preview counts too.
        assert all(e.backspaces <= len(" ~draft~") for e in sup.typed)

    def test_abort_stops_the_recorder(self, sup):
        sup._arm()
        recorder = sup.recorder
        sup.abort(reason="escape")
        assert recorder.stopped

    def test_worker_death_mid_dictation_aborts(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello"})
        sup._on_worker_exit()
        assert not sup.dictating

    def test_abort_when_idle_is_harmless(self, sup):
        sup.abort(reason="nothing to do")
        assert sup.typed == []


class TestSettings:
    def test_model_paths_are_absolute(self, sup):
        settings = sup.worker_settings()
        assert Path(settings["models"]["preview"]).is_absolute()
        assert Path(settings["models"]["final"]).is_absolute()

    def test_battery_selects_the_battery_model(self, sup, monkeypatch):
        import lwstt.supervisor as module

        sup.config.models.final_ac = "model-ac"
        sup.config.models.final_battery = "model-batt"
        monkeypatch.setattr(module, "on_battery", lambda: True)
        assert sup.worker_settings()["models"]["final"].endswith("model-batt")
        monkeypatch.setattr(module, "on_battery", lambda: False)
        assert sup.worker_settings()["models"]["final"].endswith("model-ac")

    def test_word_list_paths_are_resolved_against_the_config_dir(self, sup):
        settings = sup.worker_settings()
        assert settings["vocabulary"]["file"].endswith("vocabulary.md")
        assert Path(settings["vocabulary"]["file"]).is_absolute()

    def test_settings_are_json_serialisable(self, sup):
        import json

        json.dumps(sup.worker_settings())


class TestSessionIsolation:
    """Every keypress must start from a clean slate.

    A transcription still in flight when the keys are released can land after
    the next dictation has begun. Without a session id it gets typed into the
    new one, which looks like the previous dictation filling itself in on the
    following keypress.
    """

    def test_a_reply_from_a_finished_dictation_is_ignored(self, sup):
        sup._arm()
        sup.dictating = True
        stale = sup.session
        sup._on_worker_message(Msg.PREVIEW, {"session": stale, "text": "first"})
        assert sup.typing.on_screen() != ""

        # Second dictation begins.
        sup._disarm()
        sup._arm()
        sup.dictating = True
        sup.typed.clear()

        sup._on_worker_message(Msg.COMMIT, {"session": stale, "text": "leftover"})
        assert sup.typed == [], "text from the previous dictation leaked"
        assert "leftover" not in sup.typing.on_screen()

    def test_the_current_session_is_accepted(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.COMMIT, {"session": sup.session, "text": "Hello."})
        assert "Hello." in sup.typing.on_screen()

    def test_a_message_with_no_session_is_ignored(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"text": "unlabelled"})
        assert sup.typed == []

    def test_each_arm_advances_the_session(self, sup):
        first = sup.session
        sup._arm()
        second = sup.session
        sup._disarm()
        sup._arm()
        assert first != second != sup.session

    def test_settings_carry_the_session(self, sup):
        sup._arm()
        sup.armed_at -= 10
        sup._pump_once()
        assert sup.worker.started_with["session"] == sup.session


class TestCancelOnTyping:
    """Typing while a dictation is still finishing cancels the rest."""

    def test_cancel_stops_further_output(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "draft"})
        sup.cancel_pending("user typed")
        sup.typed.clear()
        sup._on_worker_message(Msg.COMMIT, {"session": sup.session, "text": "Too late."})
        assert sup.typed == []
        assert not sup.dictating

    def test_cancel_removes_the_markers(self, sup):
        """Runs from the keyboard hook, before the keystroke reaches the target,
        so the screen still matches our state and the count is still correct.

        Leaving them was the "tildes just stay there" complaint."""
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "draft"})
        sup.typed.clear()
        sup.cancel_pending("user typed")
        assert sup.typed != [], "provisional text must be cleared, not abandoned"
        assert sup.typing.on_screen() == ""

    def test_cancel_keeps_committed_text(self, sup):
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.COMMIT, {"session": sup.session, "text": "Kept."})
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "draft"})
        sup.typed.clear()
        sup.cancel_pending("user typed")
        # Only the provisional tail and its markers come off.
        assert all(e.backspaces <= len(" ~draft~") for e in sup.typed)

    def test_cancel_leaves_no_listening_markers(self, sup):
        sup._arm()
        sup.armed_at -= 10
        sup._pump_once()
        assert "~~" in sup.typing.on_screen()
        sup.cancel_pending("user typed")
        assert sup.typing.on_screen() == ""

    def test_cancel_tells_the_worker(self, sup):
        sup._arm()
        sup.dictating = True
        sup.cancel_pending("user typed")
        assert sup.worker.aborted

    def test_cancel_when_idle_is_harmless(self, sup):
        sup.cancel_pending("nothing running")
        assert sup.typed == []

    def test_a_keypress_while_finishing_invalidates_the_session(self, sup):
        sup._arm()
        sup.dictating = True
        before = sup.session
        sup.machine.state = type(sup.machine.state).IDLE  # released, still finishing
        sup._on_key(0x41, True)  # user types "A"
        assert sup.session != before, "in-flight replies must be invalidated at once"


class TestFlashReduction:
    def test_growth_is_always_applied(self, sup):
        sup.config.output.min_revision_interval_ms = 10_000
        sup._arm()
        sup.dictating = True
        for text in ("I", "I went", "I went to", "I went to the store"):
            sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": text})
        assert "I went to the store" in sup.typing.on_screen()

    def test_rewording_is_rate_limited(self, sup):
        sup.config.output.min_revision_interval_ms = 10_000
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "the cat sat"})
        sup.typed.clear()
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "the car sat"})
        assert sup.typed == [], "a pure rewording should not flash"

    def test_rewording_is_allowed_once_the_interval_passes(self, sup):
        sup.config.output.min_revision_interval_ms = 0
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "the cat sat"})
        sup.typed.clear()
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "the car sat"})
        assert sup.typed != []

    def test_a_commit_is_never_suppressed(self, sup):
        """Skipped rewordings are corrected by the commit, so it must always land."""
        sup.config.output.min_revision_interval_ms = 10_000
        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "the cat sat"})
        sup.typed.clear()
        sup._on_worker_message(Msg.COMMIT, {"session": sup.session, "text": "The car sat."})
        assert sup.typed != []
        assert "The car sat." in sup.typing.on_screen()


class TestPreviewWindow:
    """With preview.window_s set, previews stop after the opening seconds.
    From then on each finished chunk is typed once, and the markers after it
    say whether it is merely recording (~~) or holding speech whose text is
    still to come (~|~)."""

    def start(self, sup, window=10, elapsed=0.0):
        sup.config.preview.window_s = window
        sup.config.hotkey.hold_threshold_ms = 0
        sup._arm()
        sup._pump_once()
        sup.armed_at -= elapsed

    def say(self, sup, msg, **fields):
        sup._on_worker_message(msg, {"session": sup.session, **fields})

    def test_previews_are_typed_inside_the_window(self, sup):
        self.start(sup)
        self.say(sup, Msg.PREVIEW, text="hello")
        assert sup.typing.on_screen() == " ~hello~"

    def test_previews_are_dropped_after_the_window(self, sup):
        self.start(sup, elapsed=11)
        sup._pump_once()
        sup.typed.clear()
        self.say(sup, Msg.PREVIEW, text="hello")
        assert sup.typed == []

    def test_the_markers_stay_up_for_as_long_as_it_records(self, sup):
        self.start(sup, elapsed=11)
        sup._pump_once()
        assert sup.typing.on_screen() == " ~~"

    def test_buffered_speech_shows_between_the_markers(self, sup):
        self.start(sup, elapsed=11)
        self.say(sup, Msg.PENDING, pending=True)
        assert sup.typing.on_screen() == " ~|~"
        self.say(sup, Msg.PENDING, pending=False)
        assert sup.typing.on_screen() == " ~~"

    def test_inside_the_window_the_preview_says_it_instead(self, sup):
        self.start(sup)
        self.say(sup, Msg.PENDING, pending=True)
        assert sup.typing.on_screen() == " ~~"
        # ...but the state is not lost: it shows as soon as the window closes.
        sup.armed_at -= 11
        sup._pump_once()
        assert sup.typing.on_screen() == " ~|~"

    def test_a_preview_on_screen_waits_for_its_commit(self, sup):
        """Removing it at the deadline would be one more delete, and would
        blank the screen while the chunk is still being spoken."""
        self.start(sup)
        self.say(sup, Msg.PREVIEW, text="helo")
        self.say(sup, Msg.PENDING, pending=True)
        sup.armed_at -= 11
        sup.typed.clear()
        sup._pump_once()
        assert sup.typed == []
        self.say(sup, Msg.COMMIT, text="Hello.", pending=False)
        assert sup.typing.on_screen() == " Hello. ~~"

    def test_a_commit_with_more_behind_it_keeps_the_pending_marker(self, sup):
        self.start(sup, elapsed=11)
        self.say(sup, Msg.PENDING, pending=True)
        self.say(sup, Msg.COMMIT, text="One.", pending=True)
        assert sup.typing.on_screen() == " One. ~|~"

    def test_only_the_markers_are_ever_deleted_after_the_window(self, sup):
        self.start(sup, elapsed=11)
        sup._pump_once()
        sup.typed.clear()
        for text in ("One.", "Two.", "Three."):
            self.say(sup, Msg.PENDING, pending=True)
            self.say(sup, Msg.PREVIEW, text=text.lower())
            sup._pump_once()
            self.say(sup, Msg.COMMIT, text=text, pending=False)
        assert sup.typing.on_screen() == " One. Two. Three. ~~"
        assert max(edit.backspaces for edit in sup.typed) <= len("~|~")

    def test_an_empty_chunk_leaves_the_markers_standing(self, sup):
        self.start(sup, elapsed=11)
        self.say(sup, Msg.PENDING, pending=True)
        self.say(sup, Msg.COMMIT, text="", pending=False)
        assert sup.typing.on_screen() == " ~~"

    def test_release_with_nothing_buffered_clears_the_markers(self, sup):
        self.start(sup, elapsed=11)
        sup._pump_once()
        sup._disarm()
        assert sup.typing.on_screen() == ""

    def test_the_pending_marker_outlives_the_release(self, sup):
        """It promises text, and the text is still coming."""
        self.start(sup, elapsed=11)
        self.say(sup, Msg.PENDING, pending=True)
        sup._disarm()
        assert sup.typing.on_screen() == " ~|~"
        sup.typed.clear()
        self.say(sup, Msg.COMMIT, text="One.", pending=False)
        self.say(sup, Msg.DONE)
        assert sup.typing.on_screen() == ""  # state forgotten once finished
        assert sup.typed == [Edit(3, "One.")]

    def test_done_clears_a_pending_marker_that_came_to_nothing(self, sup):
        self.start(sup, elapsed=11)
        self.say(sup, Msg.PENDING, pending=True)
        sup._disarm()
        sup.typed.clear()
        self.say(sup, Msg.DONE)
        assert sup.typed == [Edit(len(" ~|~"), "")]

    def test_a_stale_pending_report_is_ignored(self, sup):
        self.start(sup, elapsed=11)
        sup._pump_once()
        sup._on_worker_message(Msg.PENDING, {"session": sup.session - 1, "pending": True})
        assert sup.typing.on_screen() == " ~~"

    def test_no_pending_marker_without_listening_markers(self, sup):
        sup.config.output.show_listening_markers = False
        self.start(sup, elapsed=11)
        self.say(sup, Msg.PENDING, pending=True)
        self.say(sup, Msg.COMMIT, text="One.", pending=True)
        assert sup.typing.on_screen() == " One."

    def test_zero_previews_for_the_whole_dictation(self, sup):
        self.start(sup, window=0, elapsed=3600)
        sup._pump_once()
        self.say(sup, Msg.PENDING, pending=True)
        assert sup.typing.on_screen() == " ~~"
        self.say(sup, Msg.PREVIEW, text="hello")
        assert sup.typing.on_screen() == " ~hello~"

    def test_the_window_restarts_with_each_dictation(self, sup):
        self.start(sup, elapsed=11)
        self.say(sup, Msg.PENDING, pending=True)
        sup._disarm()
        self.say(sup, Msg.DONE)
        sup._arm()
        sup._pump_once()
        assert sup.typing.on_screen() == " ~~"
        self.say(sup, Msg.PREVIEW, text="hello")
        assert sup.typing.on_screen() == " ~hello~"

    def test_the_worker_is_told_the_window(self, sup):
        sup.config.preview.window_s = 10
        assert sup.worker_settings()["preview"]["window_s"] == 10


class TestTransientFocusLoss:
    """Windows reports no foreground window at all during app switching, while
    a menu opens, or as one window is torn down before the next is raised.

    Treating that blink as a focus change aborts a dictation the user is still
    speaking -- and since they are still holding the keys, the rest of the press
    goes nowhere and is discarded on release. Observed in the wild as "it stops
    working after I switch apps".
    """

    def test_a_null_foreground_window_does_not_abort(self, sup, monkeypatch):
        import lwstt.supervisor as module

        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello"})
        monkeypatch.setattr(module, "foreground_window", lambda: 0)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello there"})
        assert sup.dictating, "a transient loss of foreground must not abort"

    def test_typing_resumes_when_focus_returns(self, sup, monkeypatch):
        import lwstt.supervisor as module

        sup._arm()
        sup.dictating = True
        monkeypatch.setattr(module, "foreground_window", lambda: 0)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "during"})
        monkeypatch.setattr(module, "foreground_window", lambda: 12345)
        sup.typed.clear()
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "during the blink"})
        assert sup.typed != []
        assert sup.dictating

    def test_a_real_different_window_holds_output(self, sup, monkeypatch):
        import lwstt.supervisor as module

        sup._arm()
        sup.dictating = True
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello"})
        sup.typed.clear()
        monkeypatch.setattr(module, "foreground_window", lambda: 99999)
        sup._on_worker_message(Msg.PREVIEW, {"session": sup.session, "text": "hello there"})
        assert sup.typed == [], "must never type into a different document"
        assert sup._cleanup_hwnd == 12345, "the owed output must be remembered"

    def test_release_after_an_abort_is_reported_as_such(self, sup):
        messages = []
        sup.log = messages.append
        sup._arm()
        sup.dictating = True
        sup.abort(reason="focus changed")
        sup._disarm()
        assert any("after an abort" in m for m in messages)
        assert not any("discarded a" in m for m in messages)

    def test_a_genuine_stray_tap_is_still_reported_as_discarded(self, sup):
        messages = []
        sup.log = messages.append
        sup._arm()
        sup._disarm()
        assert any("discarded a" in m for m in messages)
