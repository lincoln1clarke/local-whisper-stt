"""The always-resident half.

Owns the keyboard hook, audio capture, the typing state machine, the recording
indicator and the worker's lifecycle. Standard library and ctypes only -- no
numpy, no sounddevice, no ML imports, ever. ``tests/test_architecture.py``
enforces that.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from .core.config import Config
from .core.hotkey_state import HotkeyMachine, Signal
from .core.protocol import Msg
from .core.typing_state import TypingState
from .win import keys, sendinput
from .win.audio import Recorder
from .win.hook import KeyboardHook
from .win.indicator import RecordingDot
from .win.system import (
    ARM_TONE,
    COMMIT_TONE,
    ERROR_TONE,
    beep,
    foreground_window,
    on_battery,
    window_title,
)

AUDIO_PUMP_INTERVAL = 0.05
# How long to keep trying to clean markers stranded by a focus change.
CLEANUP_GRACE_S = 120.0
IDLE_CHECK_INTERVAL = 5.0


class Supervisor:
    def __init__(self, config: Config, config_dir: Path, log=print) -> None:
        self.config = config
        self.config_dir = config_dir
        self.log = log

        self.combo = keys.resolve_combo(config.hotkey.keys)
        self.machine = HotkeyMachine(self.combo)
        self.typing = TypingState(
            config.output.marker_open,
            config.output.marker_close,
            leading_space=config.output.leading_space,
        )
        self.dot = RecordingDot()
        self.recorder: Recorder | None = None
        self.hook: KeyboardHook | None = None

        from .worker_client import WorkerClient

        self.worker = WorkerClient(
            on_message=self._on_worker_message,
            on_exit=self._on_worker_exit,
            log=lambda m: self.log(f"worker: {m}"),
        )

        self.armed_at = 0.0
        self.session = 0
        self._last_revision = 0.0
        self._aborted_this_press = False
        self._focus_warned = False
        self._awaiting_done_since: float | None = None
        self._cleanup_hwnd = 0
        self._cleanup_deadline = 0.0
        self.target_hwnd = 0
        self.threshold_passed = False
        self.dictating = False
        self.stopping = threading.Event()
        self._signals: "queue.Queue[Signal]" = queue.Queue()
        self._lock = threading.RLock()
        self._pump_thread: threading.Thread | None = None
        self._idle_thread: threading.Thread | None = None
        self._signal_thread: threading.Thread | None = None

    # -- settings handed to the worker -----------------------------------

    def worker_settings(self) -> dict:
        c = self.config
        battery = on_battery()
        return {
            "session": self.session,
            "sample_rate": c.audio.sample_rate,
            "language": c.final.language,
            "models": {
                "preview": c.model_path(c.models.preview),
                "final": c.model_path(c.final_model(battery)),
                "device": c.models.device,
                "compute_type": c.models.compute_type,
            },
            "chunking": {
                "silence_gap_ms": c.chunking.silence_gap_ms,
                "max_chunk_s": c.chunking.max_chunk_s,
            },
            "preview": {
                "enabled": c.preview.enabled,
                "refresh_ms": c.preview.refresh_ms,
                "beam_size": c.preview.beam_size,
            },
            "final": {
                "beam_size": c.final.beam_size,
                "condition_on_previous_text": c.final.condition_on_previous_text,
                "vad_filter": c.final.vad_filter,
                "language": c.final.language,
            },
            "vocabulary": {
                "file": str(self.config_dir / c.vocabulary.file),
                "max_tokens": c.vocabulary.max_tokens,
                "mode": c.vocabulary.mode,
                "apply_to_preview": c.vocabulary.apply_to_preview,
            },
            "filler": {
                "file": str(self.config_dir / c.filler.file),
                "enabled": c.filler.enabled,
            },
            "output": {
                "capitalize_standalone_i": c.output.capitalize_standalone_i,
            },
            "logging": {
                "enabled": c.logging.enabled,
                "dir": str(self.config_dir / c.logging.dir),
                "format": c.logging.format,
                "bitrate_kbps": c.logging.bitrate_kbps,
            },
        }

    # -- typing ----------------------------------------------------------

    def _focus_intact(self) -> bool:
        """Whether the window these characters went into still has the keyboard.

        Windows reports no foreground window at all during app switching, while
        a menu opens, or as one window is torn down before the next is raised,
        so a null handle is treated as a blink rather than a change.
        """
        current = foreground_window()
        if current == self.target_hwnd:
            return True
        if current == 0:
            # No foreground window right now. Nothing to type into and nothing
            # to be confused about; wait for it to come back.
            return True
        if not self._focus_warned:
            self._focus_warned = True
            self.log(
                f"focus moved ({window_title(self.target_hwnd)!r} [{self.target_hwnd}]"
                f" -> {window_title(current)!r} [{current}]); holding output"
            )
        return False

    def _end_dictation(self, reason: str) -> None:
        """The single way a dictation ends. Always leaves the screen clean.

        Order matters. set_listening(False) only removes the empty marker pair;
        while provisional text is showing it is a no-op, because the wrap comes
        from the provisional text rather than the listening flag. Clearing the
        provisional text first is what makes the second call able to do
        anything -- otherwise a preview that was never superseded by a commit
        stays on screen with its tildes for good. That happens whenever the
        final drain drops a silence-only tail: no commit is sent, so nothing
        replaces the last preview.
        """
        self._emit(self.typing.abort)
        self._emit(lambda: self.typing.set_listening(False))
        # Committed text staying on screen is correct -- it is the finished
        # dictation. Only markers are leftovers.
        if self.typing.provisional or self.typing.listening:
            self.log(
                f"dictation ended with markers still on screen "
                f"({self.typing.wrapped_provisional()!r}, {reason})"
            )
            self._defer_typing()
        self._release_typing_state()
        self.dictating = False
        self._awaiting_done_since = None

    def _defer_typing(self) -> None:
        """Remember that output is owed to a window that is not focused now."""
        if self.typing.on_screen() or self.dictating:
            self._cleanup_hwnd = self.target_hwnd
            self._cleanup_deadline = time.monotonic() + CLEANUP_GRACE_S

    def _release_typing_state(self) -> None:
        """Forget the on-screen state -- unless a cleanup still needs it.

        Resetting while a deferred cleanup is pending destroys the only record
        of what was left on screen, which is what stranded the markers for good.
        """
        if not self._cleanup_hwnd:
            self.typing.reset()

    def _type_edit(self, edit) -> bool:
        """Type an edit whose target has already been verified by the caller."""
        if edit.is_noop:
            return True
        try:
            sendinput.apply_edit(edit.backspaces, edit.text)
        except OSError as exc:
            self.log(f"typing failed: {exc}")
            return False
        return True

    def _emit(self, mutate) -> bool:
        """Check the target is still ours, *then* mutate and type.

        The order matters twice over. The typing state records what is on
        screen, so mutating it for an edit that is then refused makes every
        later edit wrong -- the counts stop describing the document. And because
        nothing is mutated when the check fails, state and screen stay in step,
        so typing simply resumes where it left off if focus comes back.

        A focus change no longer aborts. Windows hands focus around for all
        sorts of momentary reasons -- a tooltip, a trackpad tap, another app
        blinking to the front -- and killing a dictation the user is still
        speaking is far worse than pausing its output for a moment.
        """
        if not self._focus_intact():
            self._defer_typing()
            return False
        edit = mutate()
        if edit.is_noop:
            return True
        try:
            sendinput.apply_edit(edit.backspaces, edit.text)
        except OSError as exc:
            self.log(f"typing failed: {exc}")
            return False
        return True

    # -- worker messages -------------------------------------------------

    def _is_current(self, obj: dict) -> bool:
        """Reject replies belonging to a dictation that has already finished.

        A transcription in flight when the keys are released can land after the
        next dictation has begun. Without this check it gets typed into the new
        one, which looks like the previous dictation filling itself in on the
        following keypress.
        """
        return int(obj.get("session", -1)) == self.session

    def _on_worker_message(self, msg: Msg, obj: dict) -> None:
        with self._lock:
            if msg in (Msg.PREVIEW, Msg.COMMIT, Msg.DONE) and not self._is_current(obj):
                return
            if msg in (Msg.PREVIEW, Msg.COMMIT) and not self.dictating:
                return

            if msg is Msg.PREVIEW:
                self._apply_preview(obj.get("text", ""))
            elif msg is Msg.COMMIT:
                text = obj.get("text", "")
                if text:
                    self._emit(lambda: self.typing.commit(text))
                    if self.config.feedback.beep_on_commit:
                        beep(*COMMIT_TONE)
                else:
                    self._emit(self.typing.abort)
            elif msg is Msg.DONE:
                self._end_dictation("done")
            elif msg is Msg.ERROR:
                self.log(f"worker error: {obj.get('message')}")
                if self.config.feedback.beep_on_error:
                    beep(*ERROR_TONE)

    def _apply_preview(self, text: str) -> None:
        """Type a preview update, skipping churn that is not worth the flicker.

        Greedy decoding rewrites its own tail constantly. Every rewrite is a
        visible delete-and-retype, and most of them replace a few words with
        near-identical ones. Growth is free -- appending costs no backspaces --
        so it is always applied; rewrites are rate-limited, and the commit
        corrects anything skipped.
        """
        if self.typing.preview_edit(text).is_noop:
            return
        # Compare the provisional *text*, not the screen edit: growing the
        # preview still rewrites the closing marker, so every update looks like
        # a revision at the screen level.
        is_growth = text.startswith(self.typing.provisional)
        if not is_growth:
            now = time.monotonic()
            interval = self.config.output.min_revision_interval_ms / 1000.0
            if now - self._last_revision < interval:
                return
            self._last_revision = now
        self._emit(lambda: self.typing.set_provisional(text))

    def cancel_pending(self, reason: str) -> None:
        """Stop producing output for a dictation that is still finishing.

        The markers are removed here rather than abandoned. This runs from the
        keyboard hook, which fires *before* the keystroke reaches the target, so
        at this moment the screen still matches the typing state exactly and the
        backspace count is still correct. Leaving them behind was the "tildes
        just stay there" complaint; committed text is kept, as always.
        """
        with self._lock:
            if not self.dictating:
                return
            self._end_dictation(f"cancelled: {reason}")
            self.worker.abort()
            self.log(f"cancelled pending output: {reason}")

    def _on_worker_exit(self) -> None:
        with self._lock:
            if self.dictating:
                self.log("worker died mid-dictation")
                self._finish_locally("worker exited")

    # -- hotkey ----------------------------------------------------------

    def _on_key(self, vk: int, is_down: bool) -> bool:
        """Runs inside the low-level hook. Must never block.

        A blocked WH_KEYBOARD_LL callback stalls keyboard input for the entire
        machine, so this only runs the state machine, replays any deferred
        modifier, and hands the signal to another thread. Nothing here waits on
        a lock the audio pump might be holding.
        """
        if is_down and not self.machine.armed and self.dictating:
            # Synchronously, not via the signal thread: the hook is called
            # before the keystroke is delivered, so backspaces injected here are
            # queued ahead of it and the screen still matches our state. Handing
            # this to another thread loses that ordering, and the markers get
            # stranded behind the user's own text.
            self.session += 1
            try:
                self.cancel_pending("user started typing")
            except Exception as exc:
                self.log(f"cancel failed: {exc}")

        decision = self.machine.on_key(vk, is_down)

        for inject_vk, inject_down in decision.inject:
            try:
                event = (
                    sendinput.vk_down(inject_vk)
                    if inject_down
                    else sendinput.vk_up(inject_vk)
                )
                sendinput.send([event])
            except OSError:
                pass

        if decision.signal is not Signal.NONE:
            self._signals.put(decision.signal)

        return decision.swallow

    def _try_deferred_cleanup(self) -> None:
        """Remove markers stranded by a focus change, once focus returns."""
        with self._lock:
            if not self._cleanup_hwnd:
                return
            if time.monotonic() > self._cleanup_deadline:
                self.log("gave up on stranded markers: focus never came back")
                self._cleanup_hwnd = 0
                self.typing.reset()
                return
            if foreground_window() != self._cleanup_hwnd:
                return
            self._cleanup_hwnd = 0
            if self.dictating:
                # Still speaking. Typing resumes on its own from the state that
                # was never mutated while focus was away; nothing to clean.
                self._focus_warned = False
                self.log("focus returned; resuming output")
                return
            self._type_edit(self.typing.set_listening(False))
            self._type_edit(self.typing.abort())
            self.typing.reset()
            self.log("cleared markers stranded by a focus change")

    def _signal_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                signal = self._signals.get(timeout=0.1)
            except queue.Empty:
                self._try_deferred_cleanup()
                continue
            try:
                if signal is Signal.ARM:
                    self._arm()
                elif signal is Signal.DISARM:
                    self._disarm()
                elif signal is Signal.ABORT:
                    self.abort(reason="escape")
            except Exception as exc:
                self.log(f"signal {signal.name} failed: {exc}")

    def _arm(self) -> None:
        with self._lock:
            self.armed_at = time.monotonic()
            self._try_deferred_cleanup()
            self._cleanup_hwnd = 0
            self._awaiting_done_since = None
            self.session += 1
            self._aborted_this_press = False
            self._focus_warned = False
            # Measured from arming, so the very first reword is rate-limited
            # like any other rather than passing for free.
            self._last_revision = time.monotonic()
            self.threshold_passed = False
            self.target_hwnd = foreground_window()
            self.typing.reset()
            try:
                self.recorder = Recorder(
                    sample_rate=self.config.audio.sample_rate,
                    device=self.config.audio.input_device,
                )
                self.recorder.start()
            except Exception as exc:
                self.log(f"could not open the microphone: {exc}")
                if self.config.feedback.beep_on_error:
                    beep(*ERROR_TONE)
                self.recorder = None
                return
            if self.config.feedback.beep_on_arm:
                beep(*ARM_TONE)
            if self.config.feedback.recording_dot:
                self.dot.show()

    def _stop_recording(self) -> bytes:
        self.dot.hide()
        if not self.recorder:
            return b""
        # stop() first, then read: stopping flushes the buffer the driver was
        # still filling into the queue. Reading before it drops that tail, which
        # is the word being spoken as the keys are released.
        self.recorder.stop()
        tail = self.recorder.read_available()
        self.recorder = None
        return tail

    def _disarm(self) -> None:
        with self._lock:
            held = time.monotonic() - self.armed_at
            tail = self._stop_recording()
            if not self.threshold_passed:
                if self._aborted_this_press:
                    self.log(f"press ended {held * 1000:.0f} ms after an abort")
                else:
                    # A stray tap. Nothing was typed, so nothing to undo.
                    self.log(f"discarded a {held * 1000:.0f} ms press")
                return
            if tail:
                self.worker.send_audio(tail)
            if self.worker.end_dictation():
                self._awaiting_done_since = time.monotonic()
            else:
                # The worker is gone; no DONE is coming.
                self.log("worker unreachable on release; finishing locally")
                self._finish_locally("worker unreachable")

    def abort(self, reason: str = "", restore: bool = True) -> None:
        with self._lock:
            self._stop_recording()
            if restore:
                edit = self.typing.abort()
                self._type_edit(edit)
                self.typing.reset()
            elif self.typing.on_screen():
                # Cannot type now -- something else has the keyboard, and
                # backspacing would eat text in a document these characters
                # never went into. Remember what is stranded and clean it up if
                # focus comes back, rather than abandoning it on screen.
                self._cleanup_hwnd = self.target_hwnd
                self._cleanup_deadline = time.monotonic() + CLEANUP_GRACE_S
            else:
                self.typing.reset()
            if self.dictating:
                self.worker.abort()
            self.dictating = False
            self.threshold_passed = False
            self._awaiting_done_since = None
            self._aborted_this_press = True
            if reason:
                self.log(f"aborted: {reason}")
            if self.config.feedback.beep_on_error:
                beep(*ERROR_TONE)

    # -- background loops ------------------------------------------------

    def _pump_once(self) -> None:
        """One iteration of the audio pump. Split out so it is testable."""
        with self._lock:
            recorder = self.recorder
            if recorder is None:
                return
            held = time.monotonic() - self.armed_at
            if not self.threshold_passed:
                if held * 1000 < self.config.hotkey.hold_threshold_ms:
                    return
                # Threshold cleared: this is a real dictation. Nothing is typed
                # before this point, so a discarded tap leaves nothing behind.
                self.threshold_passed = True
                self.dictating = True
                self.worker.start_dictation(self.worker_settings())
                if self.config.output.show_listening_markers:
                    self._emit(lambda: self.typing.set_listening(True))
            data = recorder.read_available()
            if data:
                self.worker.send_audio(data)
            max_seconds = self.config.runtime.max_dictation_minutes * 60
            if recorder.seconds_recorded > max_seconds:
                # A stuck key must not record forever. Commit what we have
                # rather than discarding it.
                self.log("hit max_dictation_minutes; committing what we have")
                self._disarm()

    def _pump_audio(self) -> None:
        """Stream captured audio to the worker while the keys are held."""
        while not self.stopping.is_set():
            time.sleep(AUDIO_PUMP_INTERVAL)
            try:
                self._pump_once()
            except Exception as exc:
                self.log(f"audio pump error: {exc}")

    def _finish_locally(self, reason: str) -> None:
        """End a dictation the worker will never finish.

        Clears the markers off screen so the user is not left with stray tildes
        and no explanation, and keeps committed text, which is already final.
        """
        with self._lock:
            self._end_dictation(f"no worker: {reason}")
            self.log(f"finished without the worker: {reason}")
            if self.config.feedback.beep_on_error:
                beep(*ERROR_TONE)

    def _check_finalize_timeout(self) -> None:
        with self._lock:
            started = self._awaiting_done_since
            if started is None:
                return
            waited = time.monotonic() - started
            if waited < self.config.runtime.finalize_timeout_s:
                return
            self._finish_locally(f"no result after {waited:.0f}s")

    def _idle_watch(self) -> None:
        """Kill the worker after the configured idle period."""
        while not self.stopping.is_set():
            time.sleep(IDLE_CHECK_INTERVAL)
            self._check_finalize_timeout()
            with self._lock:
                if self.dictating or not self.worker.alive:
                    continue
                minutes = self.config.idle_exit_minutes(on_battery())
                if self.worker.idle_seconds() > minutes * 60:
                    self.log(f"worker idle for {minutes} min; stopping it")
                    self.worker.stop()

    # -- run -------------------------------------------------------------

    def start(self) -> None:
        # The dot must be created on the thread that pumps messages: a
        # window is owned by its creating thread, and one owned by a thread
        # with no message loop never paints. start() runs on the same thread
        # that goes on to call hook.pump().
        if self.config.feedback.recording_dot:
            try:
                self.dot.create()
            except OSError as exc:
                self.log(f"recording dot unavailable: {exc}")
        self.hook = KeyboardHook(self._on_key)
        self.hook.install()
        self._pump_thread = threading.Thread(
            target=self._pump_audio, name="lwstt-pump", daemon=True
        )
        self._pump_thread.start()
        self._idle_thread = threading.Thread(
            target=self._idle_watch, name="lwstt-idle", daemon=True
        )
        self._idle_thread.start()
        self._signal_thread = threading.Thread(
            target=self._signal_loop, name="lwstt-signals", daemon=True
        )
        self._signal_thread.start()
        if self.config.runtime.preload_on_start:
            self.worker.start()

    def run(self) -> None:
        self.start()
        try:
            self.hook.pump()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.stopping.set()
        if self.recorder:
            try:
                self.recorder.stop()
            except Exception:
                pass
        self.dot.destroy()
        self.worker.stop()
        if self.hook:
            self.hook.uninstall()
