"""Shared fakes for supervisor tests.

The Windows edges are replaced so the sequencing logic is testable without a
keyboard, a microphone or a GPU. ``test_supervisor.py`` defines its own copies
of these; that local definition simply shadows this one, which keeps that file
readable on its own while letting other modules reuse the fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lwstt.core.config import Config
from lwstt.core.diff import Edit

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
    monkeypatch.setattr(module, "RecordingDot", FakeDot)
    monkeypatch.setattr(
        module.sendinput,
        "apply_edit",
        lambda backspaces, text: typed.append(Edit(backspaces, text)),
    )

    supervisor = module.Supervisor(Config(), ROOT, log=lambda m: None)
    supervisor.worker = FakeWorker()
    supervisor.config.output.min_revision_interval_ms = 0
    supervisor.typed = typed
    return supervisor
