"""Dictation logging: it must never delay, or corrupt, the protocol."""

from pathlib import Path

class TestFfmpegNeverTouchesTheProtocolPipe:
    """The worker's stdin *is* the supervisor's pipe.

    ffmpeg reads stdin for interactive keys, so a child that inherits it blocks
    until its timeout while consuming framing bytes. That produced both a
    dictation that never finished (DONE delayed past two minutes, leaving "~~"
    on screen) and "unknown message type 0" when the stolen bytes desynced the
    stream. Measured on a real 4.5 min dictation: >120 s before, 2.8 s after.
    """

    def _spawn_args(self, monkeypatch):
        from lwstt.worker import audio_log

        captured = {}

        class Completed:
            returncode = 0

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            # encode_opus checks the output exists before reporting success.
            Path(command[-1]).write_bytes(b"opus")
            return Completed()

        monkeypatch.setattr(audio_log.subprocess, "run", fake_run)
        return captured

    def test_stdin_is_devnull(self, monkeypatch, tmp_path):
        import subprocess

        from lwstt.worker import audio_log

        captured = self._spawn_args(monkeypatch)
        wav = audio_log.write_wav(tmp_path / "a.wav", b"\x00\x00" * 100)
        audio_log.encode_opus(wav)
        assert captured["kwargs"].get("stdin") is subprocess.DEVNULL

    def test_nostdin_flag_is_passed(self, monkeypatch, tmp_path):
        from lwstt.worker import audio_log

        captured = self._spawn_args(monkeypatch)
        wav = audio_log.write_wav(tmp_path / "a.wav", b"\x00\x00" * 100)
        audio_log.encode_opus(wav)
        assert "-nostdin" in captured["command"]


class TestDoneOutrunsLogging:
    def test_end_replies_before_it_writes_anything(self):
        """DONE must not queue behind the WAV write and the encode.

        Those cost roughly a fifth of a second per minute of speech, and the
        markers stay on screen for every bit of it.
        """
        source = Path("lwstt/worker/__main__.py").read_text(encoding="utf-8")
        end_block = source.split("elif msg is Msg.END:")[1].split("elif msg is")[0]
        assert end_block.index("Msg.DONE") < end_block.index("spawn_log")

    def test_logging_runs_off_the_reply_thread(self):
        source = Path("lwstt/worker/__main__.py").read_text(encoding="utf-8")
        spawn = source.split("def spawn_log")[1].split("\n    def ")[0]
        assert "Thread(" in spawn and "daemon=True" in spawn
