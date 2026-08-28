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
IDLE_CHECK_INTERVAL = 5.0


class Supervisor:
    def __init__(self, config: Config, config_dir: Path, log=print) -> None:
        self.config = config
        self.config_dir = config_dir
        self.log = log

        self.combo = keys.resolve_combo(config.hotkey.keys)
        self.machine = HotkeyMachine(self.combo)
        self.typing = TypingState(config.output.marker_open, config.output.marker_close)
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
            },
            "filler": {
                "file": str(self.config_dir / c.filler.file),
                "enabled": c.filler.enabled,
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
        """Never backspace into a document the earlier characters never entered."""
        current = foreground_window()
        if current == self.target_hwnd:
            return True
        self.log(
            f"focus changed ({window_title(self.target_hwnd)!r} -> "
            f"{window_title(current)!r}); aborting"
        )
        return False

    def _apply(self, edit) -> bool:
        if edit.is_noop:
            return True
        if not self._focus_intact():
            self.abort(reason="focus changed", restore=False)
            return False
        try:
            sendinput.apply_edit(edit.backspaces, edit.text)
        except OSError as exc:
            self.log(f"typing failed: {exc}")
            return False
        return True

    # -- worker messages -------------------------------------------------

    def _on_worker_message(self, msg: Msg, obj: dict) -> None:
        with self._lock:
            if not self.dictating and msg in (Msg.PREVIEW, Msg.COMMIT):
                return
            if msg is Msg.PREVIEW:
                self._apply(self.typing.set_provisional(obj.get("text", "")))
            elif msg is Msg.COMMIT:
                text = obj.get("text", "")
                if text:
                    self._apply(self.typing.commit(text))
                    if self.config.feedback.beep_on_commit:
                        beep(*COMMIT_TONE)
                else:
                    self._apply(self.typing.abort())
            elif msg is Msg.DONE:
                self.dictating = False
                self.typing.reset()
            elif msg is Msg.ERROR:
                self.log(f"worker error: {obj.get('message')}")
                if self.config.feedback.beep_on_error:
                    beep(*ERROR_TONE)

    def _on_worker_exit(self) -> None:
        with self._lock:
            if self.dictating:
                self.log("worker died mid-dictation")
                self.abort(reason="worker exited")

    # -- hotkey ----------------------------------------------------------

    def _on_key(self, vk: int, is_down: bool) -> bool:
        """Runs inside the low-level hook. Must never block.

        A blocked WH_KEYBOARD_LL callback stalls keyboard input for the entire
        machine, so this only runs the state machine, replays any deferred
        modifier, and hands the signal to another thread. Nothing here waits on
        a lock the audio pump might be holding.
        """
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

    def _signal_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                signal = self._signals.get(timeout=0.1)
            except queue.Empty:
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
        tail = self.recorder.read_available()
        self.recorder.stop()
        self.recorder = None
        return tail

    def _disarm(self) -> None:
        with self._lock:
            held = time.monotonic() - self.armed_at
            tail = self._stop_recording()
            if not self.threshold_passed:
                # A stray tap. Nothing was typed, so nothing to undo.
                self.log(f"discarded a {held * 1000:.0f} ms press")
                return
            if tail:
                self.worker.send_audio(tail)
            self.worker.end_dictation()

    def abort(self, reason: str = "", restore: bool = True) -> None:
        with self._lock:
            self._stop_recording()
            if restore:
                edit = self.typing.abort()
                if not edit.is_noop:
                    try:
                        sendinput.apply_edit(edit.backspaces, edit.text)
                    except OSError:
                        pass
            self.typing.reset()
            if self.dictating:
                self.worker.abort()
            self.dictating = False
            self.threshold_passed = False
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

    def _idle_watch(self) -> None:
        """Kill the worker after the configured idle period."""
        while not self.stopping.is_set():
            time.sleep(IDLE_CHECK_INTERVAL)
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
