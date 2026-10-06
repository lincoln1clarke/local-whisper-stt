"""Worker process entry point.

Reads framed messages on stdin, writes framed messages on stdout. Holds the
models, the VAD, chunk-boundary detection, both transcription passes and
logging. Killed outright when idle, which returns all of its RAM and VRAM --
the reason the split exists at all.

Threading: the reader thread only moves bytes, so pipe backpressure never
depends on how long a transcription takes. All model work happens on the
processing thread, which also serialises rolling finals -- two chunks closing in
quick succession must not run concurrently on one model instance.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from ..core.chunker import Action, decide
from ..core.protocol import FrameReader, Msg, decode_json, encode, encode_json
from ..core.textproc import (
    capitalize_standalone_i,
    normalize_newlines,
    strip_fillers,
)
from ..core.wordlists import build_prompt, fit_to_budget, load_list
from . import asr, audio_log

BYTES_PER_SAMPLE = 2
PROCESS_INTERVAL = 0.1  # how often VAD + chunk decisions run
# How long to hold shutdown open for a dictation log still being written.
# Encoding costs ~1 s per five minutes of speech, so this covers any plausible
# dictation. It must stay below the supervisor's patience in WorkerClient.stop,
# or the process gets killed mid-write and the recording is lost anyway.
LOG_DRAIN_TIMEOUT_S = 10.0
# VAD boundaries are unpadded (see asr.speech_segments), so extend the cut a
# little to avoid clipping the tail of the last word.
CUT_PAD_S = 0.25


def log(message: str) -> None:
    print(f"[worker] {message}", file=sys.stderr, flush=True)


class Worker:
    def __init__(self) -> None:
        self.out = sys.stdout.buffer
        self.out_lock = threading.Lock()
        self.inbox: "queue.Queue[tuple[Msg, bytes]]" = queue.Queue()
        self.preview_slot: asr.ModelSlot | None = None
        self.final_slot: asr.ModelSlot | None = None
        self.settings: dict = {}
        self.running = True
        self.reset_dictation()

    # -- plumbing --------------------------------------------------------

    def send(self, msg: Msg, payload: bytes = b"") -> None:
        with self.out_lock:
            self.out.write(encode(msg, payload))
            self.out.flush()

    def send_json(self, msg: Msg, obj) -> None:
        with self.out_lock:
            self.out.write(encode_json(msg, obj))
            self.out.flush()

    def reset_dictation(self) -> None:
        self.open_pcm = bytearray()  # audio for the chunk currently being spoken
        self.all_pcm = bytearray()  # whole dictation, for the log
        self.chunk_index = 0
        self.committed: list[str] = []
        self.segments_log: list[dict] = []
        self.dictating = False
        self.ended = False
        self.last_preview = 0.0
        self.last_preview_text = ""
        self.pending: bool | None = False  # last state reported; None forces a resend
        self.started_at = None
        self.session = 0
        self.chunk_offset = 0.0  # seconds of this dictation already committed

    # -- reader thread ---------------------------------------------------

    def read_stdin(self) -> None:
        reader = FrameReader()
        stream = sys.stdin.buffer
        try:
            while True:
                # read1, not read: BufferedReader.read(n) blocks until it
                # has exactly n bytes, which deadlocks on a short message.
                data = stream.read1(8192)
                if not data:
                    break
                for frame in reader.feed(data):
                    self.inbox.put(frame)
        except Exception as exc:  # pragma: no cover - pipe teardown
            log(f"reader stopped: {exc}")
        finally:
            self.inbox.put((Msg.SHUTDOWN, b""))

    # -- settings --------------------------------------------------------

    @property
    def sample_rate(self) -> int:
        return int(self.settings.get("sample_rate", 16000))

    def seconds(self, pcm: bytes | bytearray) -> float:
        return len(pcm) / (self.sample_rate * BYTES_PER_SAMPLE)

    def ensure_models(self) -> None:
        models = self.settings.get("models", {})
        device = models.get("device", "cuda")
        compute = models.get("compute_type", "float16")
        if self.preview_slot is None or self.preview_slot.path != models.get("preview"):
            self.preview_slot = asr.ModelSlot(models["preview"], device, compute)
        if models.get("final") == models.get("preview"):
            # Same weights for both passes: share one slot rather than loading a
            # second copy. Halves VRAM and skips a redundant model load.
            self.final_slot = self.preview_slot
        elif self.final_slot is None or self.final_slot.path != models.get("final"):
            self.final_slot = asr.ModelSlot(models["final"], device, compute)

    def vocabulary_prompt(self) -> str:
        vocab = self.settings.get("vocabulary", {})
        path = vocab.get("file")
        if not path:
            return ""
        terms = load_list(path)
        if not terms:
            return ""
        counter = asr.count_tokens_with(self.final_slot) if self.final_slot else None
        kept = (
            fit_to_budget(terms, int(vocab.get("max_tokens", 224)), counter)
            if counter
            else fit_to_budget(terms, int(vocab.get("max_tokens", 224)))
        )
        return build_prompt(kept)

    def preview_hotwords(self) -> str | None:
        """Vocabulary bias for the preview pass -- off by default.

        Whisper emits initial-prompt tokens into its output often enough to
        matter, and greedy decoding on a partial phrase is where it happens
        most. The words appear, then the final pass replaces them; harmless but
        alarming to watch. The final pass keeps the bias.
        """
        if not self.settings.get("vocabulary", {}).get("apply_to_preview", False):
            return None
        return self.settings.get("_hotwords") or None

    def fillers(self) -> list[str]:
        cfg = self.settings.get("filler", {})
        if not cfg.get("enabled", True):
            return []
        return load_list(cfg.get("file", "filler.md"))

    # -- transcription ---------------------------------------------------

    def clean(self, text: str) -> str:
        text = normalize_newlines(text)
        text = strip_fillers(text, self.fillers())
        if self.settings.get("output", {}).get("capitalize_standalone_i", True):
            text = capitalize_standalone_i(text)
        return text.strip()

    def run_preview(self) -> None:
        preview_cfg = self.settings.get("preview", {})
        if not preview_cfg.get("enabled", True):
            return
        # Past the window the supervisor would only throw the result away, and
        # the pass would still have cost the finals ~0.3 s of GPU. Measured in
        # audio received rather than on a clock, so a replayed recording
        # behaves the same at any speed.
        window = preview_cfg.get("window_s", 0)
        if window and self.seconds(self.all_pcm) >= window:
            return
        now = time.monotonic()
        if (now - self.last_preview) * 1000 < preview_cfg.get("refresh_ms", 400):
            return
        self.last_preview = now

        audio = asr.pcm_to_float(bytes(self.open_pcm))
        if audio.size < self.sample_rate * 0.3:
            return
        if not asr.speech_segments(audio, self.sample_rate):
            return
        try:
            segments = asr.transcribe(
                self.preview_slot,
                audio,
                language=self.settings.get("language", "en"),
                beam_size=preview_cfg.get("beam_size", 1),
                hotwords=self.preview_hotwords(),
                vad_filter=False,  # already gated above; avoids double work
            )
        except Exception as exc:
            log(f"preview failed: {exc}")
            return
        text = self.clean(asr.segments_text(segments))
        if text and text != self.last_preview_text:
            self.last_preview_text = text
            self.send_json(Msg.PREVIEW, {"text": text, "session": self.session})

    def report_pending(self, pending: bool) -> None:
        """Tell the supervisor whether speech is buffered and not yet committed.

        Sent on change only. Once previews stop, this is all the supervisor has
        to show that something was heard and its text is on the way.
        """
        if pending == self.pending:
            return
        self.pending = pending
        self.send_json(Msg.PENDING, {"pending": pending, "session": self.session})

    def buffered_speech(self) -> bool:
        audio = asr.pcm_to_float(bytes(self.open_pcm))
        return bool(audio.size and asr.speech_segments(audio, self.sample_rate))

    def close_chunk(self, cut_at: float, forced: bool) -> None:
        """Finalise everything up to ``cut_at`` and emit it as committed."""
        cut_bytes = int((cut_at + CUT_PAD_S) * self.sample_rate) * BYTES_PER_SAMPLE
        cut_bytes = min(cut_bytes, len(self.open_pcm))
        if cut_bytes <= 0:
            return
        head = bytes(self.open_pcm[:cut_bytes])
        del self.open_pcm[:cut_bytes]

        audio = asr.pcm_to_float(head)
        final_cfg = self.settings.get("final", {})
        try:
            segments = asr.transcribe(
                self.final_slot,
                audio,
                language=final_cfg.get("language", "en"),
                beam_size=final_cfg.get("beam_size", 5),
                hotwords=self.settings.get("_hotwords"),
                condition_on_previous_text=final_cfg.get(
                    "condition_on_previous_text", False
                ),
                vad_filter=final_cfg.get("vad_filter", True),
            )
        except Exception as exc:
            log(f"final failed: {exc}\n{traceback.format_exc()}")
            self.send_json(Msg.ERROR, {"message": f"transcription failed: {exc}"})
            # No commit goes out to carry the new state, so say it again.
            self.pending = None
            return

        text = self.clean(asr.segments_text(segments))
        for segment in segments:
            self.segments_log.append(
                {
                    "start": round(self.chunk_offset + segment.start, 3),
                    "end": round(self.chunk_offset + segment.end, 3),
                    "text": segment.text,
                }
            )
        self.chunk_offset += self.seconds(head)
        self.last_preview_text = ""

        if text:
            self.committed.append(text)
        # The commit carries whether more speech is already waiting behind it,
        # so the supervisor types the text and the right marker in one edit.
        # Sent even when nothing survived, to clear whatever is on screen.
        self.pending = self.buffered_speech()
        self.send_json(
            Msg.COMMIT,
            {
                "chunk": self.chunk_index,
                "text": text,
                "forced": forced,
                "pending": self.pending,
                "session": self.session,
            },
        )
        self.chunk_index += 1

    def process_audio(self) -> None:
        if not self.dictating:
            return
        audio = asr.pcm_to_float(bytes(self.open_pcm))
        buffer_s = self.seconds(self.open_pcm)
        chunking = self.settings.get("chunking", {})
        gap_s = chunking.get("silence_gap_ms", 800) / 1000.0

        speech = asr.speech_segments(audio, self.sample_rate) if audio.size else []
        result = decide(
            speech,
            buffer_s,
            gap_s,
            float(chunking.get("max_chunk_s", 25.0)),
            ended=self.ended,
        )

        if result.action is Action.DROP:
            # Silence only. Never hand this to the model.
            self.open_pcm.clear()
            self.last_preview_text = ""
            self.report_pending(False)
            return
        if result.action is Action.CLOSE:
            self.close_chunk(result.cut_at, result.forced)
            return
        self.report_pending(bool(speech))
        self.run_preview()

    # -- logging ---------------------------------------------------------

    def spawn_log(self) -> None:
        """Write this dictation's log on a background thread.

        Takes its own snapshot of the audio, since reset_dictation clears the
        buffer as soon as this returns.
        """
        log_cfg = self.settings.get("logging", {})
        if not log_cfg.get("enabled", True) or not self.all_pcm:
            return
        pcm = bytes(self.all_pcm)
        segments = list(self.segments_log)
        text = " ".join(self.committed)
        settings = {k: v for k, v in self.settings.items() if not k.startswith("_")}
        thread = threading.Thread(
            target=self.write_log,
            args=(pcm, segments, text, settings),
            name="lwstt-worker-log",
            daemon=True,
        )
        self._log_threads = [t for t in getattr(self, "_log_threads", []) if t.is_alive()]
        self._log_threads.append(thread)
        thread.start()

    def finish_logs(self, timeout: float = 30.0) -> None:
        """Wait for in-flight log writes, up to ``timeout`` in total."""
        deadline = time.monotonic() + timeout
        for thread in getattr(self, "_log_threads", []):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log("gave up waiting for a dictation log to finish writing")
                return
            thread.join(remaining)

    def write_log(self, pcm, segments, text, settings) -> None:
        log_cfg = self.settings.get("logging", {})
        try:
            day, stem = audio_log.dictation_paths(log_cfg.get("dir", "logs"))
            wav_path = day / f"{stem}.wav"
            audio_log.write_wav(wav_path, pcm, self.sample_rate)
            audio_log.write_metadata(
                day / f"{stem}.json",
                {
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                    "duration_s": round(self.seconds(pcm), 3),
                    "text": text,
                    "segments": segments,
                    "settings": settings,
                },
            )
            if log_cfg.get("format", "opus") == "opus":
                audio_log.encode_opus(wav_path, int(log_cfg.get("bitrate_kbps", 24)))
        except Exception as exc:
            log(f"logging failed: {exc}")

    # -- message handling ------------------------------------------------

    def handle(self, msg: Msg, payload: bytes) -> None:
        if msg is Msg.START:
            settings = decode_json(payload)
            self.reset_dictation()
            self.settings = settings
            self.session = int(settings.get("session", 0))
            self.dictating = True
            self.started_at = time.monotonic()
            self.ensure_models()
            self.settings["_hotwords"] = self.vocabulary_prompt()
            self.send_json(Msg.READY, {"ok": True, "session": self.session})

        elif msg is Msg.AUDIO:
            if self.dictating:
                self.open_pcm.extend(payload)
                self.all_pcm.extend(payload)

        elif msg is Msg.END:
            self.ended = True
            # Drain whatever is left, one chunk at a time.
            guard = 0
            while self.open_pcm and guard < 200:
                before = len(self.open_pcm)
                self.process_audio()
                if len(self.open_pcm) == before:
                    break
                guard += 1
            # DONE first, logging afterwards and off-thread. Writing the WAV
            # and encoding it blocks for as long as the dictation was: ~0.3 s
            # for 30 s of speech, ~2 s for ten minutes. Doing it before DONE
            # left the markers on screen for that whole time, growing with the
            # length of the dictation.
            self.send_json(
                Msg.DONE, {"text": " ".join(self.committed), "session": self.session}
            )
            self.spawn_log()
            self.reset_dictation()

        elif msg is Msg.ABORT:
            self.reset_dictation()

        elif msg is Msg.PING:
            self.send_json(
                Msg.PONG,
                {
                    "preview_loaded": bool(self.preview_slot and self.preview_slot.loaded),
                    "final_loaded": bool(self.final_slot and self.final_slot.loaded),
                    "dictating": self.dictating,
                },
            )

        elif msg is Msg.SHUTDOWN:
            self.running = False

    def run(self) -> None:
        threading.Thread(target=self.read_stdin, name="lwstt-worker-reader", daemon=True).start()
        last_processed = 0.0
        while self.running:
            try:
                msg, payload = self.inbox.get(timeout=0.02)
            except queue.Empty:
                pass
            else:
                try:
                    self.handle(msg, payload)
                except Exception as exc:
                    log(f"handler error for {msg}: {exc}\n{traceback.format_exc()}")
                    self.send_json(Msg.ERROR, {"message": str(exc)})

            # Deliberately on a clock rather than "whenever the inbox is empty".
            # Audio arrives every ~50 ms, so a queue-empty trigger starves under
            # a steady stream -- exactly when there is most work to do.
            now = time.monotonic()
            if now - last_processed >= PROCESS_INTERVAL:
                last_processed = now
                try:
                    self.process_audio()
                except Exception as exc:
                    log(f"processing error: {exc}\n{traceback.format_exc()}")
                    self.send_json(Msg.ERROR, {"message": str(exc)})


def main() -> int:
    worker = Worker()
    try:
        worker.run()
    except KeyboardInterrupt:
        pass
    # The log is written on a background thread now, so it can still be in
    # flight. Those threads are daemons and os._exit below would take them with
    # it, losing the recording of a dictation that was followed straight away by
    # an eviction or a shutdown. Give them a bounded moment to land.
    worker.finish_logs(timeout=LOG_DRAIN_TIMEOUT_S)
    # The reader thread is parked in a blocking read on stdin. Letting the
    # interpreter finalise around it produces "_enter_buffered_busy: could not
    # acquire lock ... at interpreter shutdown" on stderr. Nothing else is left
    # to clean up -- the logs were drained above and the OS reclaims the models
    # -- so leave immediately instead.
    try:
        sys.stdout.buffer.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
