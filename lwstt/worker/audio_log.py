"""Dictation logging: WAV on the hot path, Opus immediately after.

Every dictation is kept, for a possible future fine-tune on this specific voice.

Opus encoding is a detached ffmpeg subprocess rather than a library: ffmpeg
imports nothing, holds no RAM in this process, and lives only for the ~100 ms it
takes to encode. It runs after the text is already typed. If ffmpeg is missing,
the WAV stays and nothing else breaks.

A nightly batch job was considered and rejected -- this is a laptop, and it is
asleep at night.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
import wave
from datetime import datetime
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000


def dictation_paths(log_dir: str | Path, when: datetime | None = None) -> tuple[Path, str]:
    """Directory for today plus a unique stem, e.g. 143022-a1b2c3."""
    when = when or datetime.now()
    day = Path(log_dir) / when.strftime("%Y-%m-%d")
    stem = f"{when.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"
    return day, stem


def write_wav(path: Path, pcm: bytes, sample_rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return path


def write_metadata(path: Path, payload: dict) -> Path:
    """Transcript plus per-segment timestamps and the settings in force.

    The timestamps are what keep audio and text aligned, which any future
    training use requires.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def encode_opus(wav_path: Path, bitrate_kbps: int = 24, delete_wav: bool = True) -> bool:
    """Encode a WAV to Opus. Returns False if ffmpeg is unavailable.

    This **blocks** for roughly a fifth of a second per minute of audio, so it
    must never run on the path between the keys being released and the result
    being typed. The worker calls it from a background thread.
    """
    opus_path = wav_path.with_suffix(".opus")
    command = [
        # -nostdin, and DEVNULL below, are both load-bearing. This worker's
        # stdin is the protocol pipe from the supervisor. A child inherits it,
        # and ffmpeg reads stdin for interactive keys -- so without these it
        # blocks until the 120 s timeout while eating framing bytes out of the
        # pipe, which surfaced as a hung dictation and "unknown message type 0".
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(wav_path),
        "-c:a", "libopus", "-b:a", f"{bitrate_kbps}k",
        "-application", "voip",
        str(opus_path),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0 or not opus_path.exists():
        return False
    if delete_wav:
        try:
            wav_path.unlink()
        except OSError:
            pass
    return True
