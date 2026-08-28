"""Deferred-forwarding hotkey state machine.

The problem this solves (PLAN.md, "Deferred forwarding"): the hook cannot know
at Right-Ctrl-down whether Right Alt is coming. Forwarding it eagerly would
leave the target application believing Ctrl is held, turning dictated "hello"
into Ctrl+H, Ctrl+E, Ctrl+L.

So the first combo key is swallowed and held pending:

  * the other combo key arrives, at any delay -> armed, neither key forwarded
  * any other key arrives first -> flush the pending down, behave normally
  * released having done nothing -> inject down+up so a solo tap still registers

There is deliberately no timer. A modifier held alone with nothing else pressed
has no effect in any application, so deferring it is invisible.

While armed, *every* key is swallowed: the typing invariant (N typed == N
present) breaks just as badly from a stray keypress as from an auto-pairing
editor. That also frees Escape to abort.

This module is pure logic -- no ctypes, no Windows -- so the whole thing is
unit-testable without a keyboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class State(Enum):
    IDLE = auto()
    PENDING = auto()  # one combo key held and swallowed, undecided
    ARMED = auto()  # both combo keys held, dictation running
    PASSTHROUGH = auto()  # pending key was flushed; acting as a normal modifier


class Signal(Enum):
    NONE = auto()
    ARM = auto()  # start recording
    DISARM = auto()  # stop recording, transcribe and commit
    ABORT = auto()  # Escape while armed: discard


@dataclass
class Decision:
    """What the hook should do with one key event."""

    swallow: bool
    signal: Signal = Signal.NONE
    # Synthetic events to inject, as (vk, is_down) in order.
    inject: list[tuple[int, bool]] = field(default_factory=list)


VK_ESCAPE = 0x1B


class HotkeyMachine:
    def __init__(self, combo: set[int], escape_vk: int = VK_ESCAPE) -> None:
        if len(combo) != 2:
            raise ValueError("combo must be exactly two virtual-key codes")
        self.combo = set(combo)
        self.escape_vk = escape_vk
        self.state = State.IDLE
        self.pending_vk: int | None = None
        self.held: set[int] = set()

    @property
    def armed(self) -> bool:
        return self.state is State.ARMED

    def on_key(self, vk: int, is_down: bool) -> Decision:
        if is_down:
            self.held.add(vk)
        else:
            self.held.discard(vk)

        handler = {
            State.IDLE: self._idle,
            State.PENDING: self._pending,
            State.ARMED: self._armed,
            State.PASSTHROUGH: self._passthrough,
        }[self.state]
        return handler(vk, is_down)

    # -- per-state handlers ----------------------------------------------

    def _idle(self, vk: int, is_down: bool) -> Decision:
        if is_down and vk in self.combo:
            self.state = State.PENDING
            self.pending_vk = vk
            return Decision(swallow=True)
        return Decision(swallow=False)

    def _pending(self, vk: int, is_down: bool) -> Decision:
        assert self.pending_vk is not None

        if is_down and vk in self.combo and vk != self.pending_vk:
            self.state = State.ARMED
            return Decision(swallow=True, signal=Signal.ARM)

        if not is_down and vk == self.pending_vk:
            # Held alone and released: replay it as a genuine solo tap. Ctrl
            # alone does nothing, but a solo Alt tap focuses the menu bar and
            # some users rely on that.
            self.state = State.IDLE
            self.pending_vk = None
            return Decision(swallow=True, inject=[(vk, True), (vk, False)])

        if is_down:
            # A real shortcut is starting (RCtrl+C). Flush the held modifier so
            # the application sees it, then let this key through untouched.
            pending = self.pending_vk
            self.state = State.PASSTHROUGH
            return Decision(swallow=False, inject=[(pending, True)])

        # A key-up for something we never saw go down; nothing to decide.
        return Decision(swallow=False)

    def _armed(self, vk: int, is_down: bool) -> Decision:
        if not is_down and vk in self.combo:
            self.state = State.IDLE
            self.pending_vk = None
            return Decision(swallow=True, signal=Signal.DISARM)

        if is_down and vk == self.escape_vk:
            self.state = State.IDLE
            self.pending_vk = None
            return Decision(swallow=True, signal=Signal.ABORT)

        # Everything else is suppressed while armed.
        return Decision(swallow=True)

    def _passthrough(self, vk: int, is_down: bool) -> Decision:
        if not is_down and vk == self.pending_vk:
            self.state = State.IDLE
            self.pending_vk = None
        return Decision(swallow=False)
