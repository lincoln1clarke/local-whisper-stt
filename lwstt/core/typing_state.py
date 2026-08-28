"""The on-screen typing state machine.

Tracks exactly what this program has put on screen so every update can be
expressed as a suffix edit. The invariant the whole output design rests on:

    on_screen() == committed + wrapped(provisional)

Committed text is final -- markers removed, never rewritten. Only the
provisional tail is ever revised, which is what bounds the backspace blast
radius to one phrase regardless of how long the dictation runs.
"""

from __future__ import annotations

from .diff import Edit, diff_edit
from .textproc import append_chunk


class TypingState:
    def __init__(
        self,
        marker_open: str = "~",
        marker_close: str = "~",
        leading_space: bool = True,
    ) -> None:
        self.marker_open = marker_open
        self.marker_close = marker_close
        self.leading_space = leading_space
        self.committed = ""
        self.provisional = ""

    # -- introspection ---------------------------------------------------

    def wrapped_provisional(self) -> str:
        if not self.provisional:
            return ""
        return f"{self.marker_open}{self.provisional}{self.marker_close}"

    def on_screen(self) -> str:
        """Everything this program has typed and not removed.

        The leading space is computed here rather than stored, so it appears
        with the first character typed and disappears again on abort. Dictation
        usually starts where a caret already sits at the end of a word, and
        there is no way to read the target to find out.
        """
        body = self.committed + self.wrapped_provisional()
        if not body:
            return ""
        return (" " if self.leading_space else "") + body

    def preview_edit(self, text: str) -> Edit:
        """What set_provisional(text) would emit, without changing anything.

        Lets the caller decide whether an update is worth the visible rewrite.
        """
        before = self.on_screen()
        saved = self.provisional
        self.provisional = text
        after = self.on_screen()
        self.provisional = saved
        return diff_edit(before, after)

    # -- transitions -----------------------------------------------------

    def _transition(self, mutate) -> Edit:
        before = self.on_screen()
        mutate()
        return diff_edit(before, self.on_screen())

    def set_provisional(self, text: str) -> Edit:
        """Replace the provisional tail. Returns the edit to apply."""

        def mutate() -> None:
            self.provisional = text

        return self._transition(mutate)

    def commit(self, text: str) -> Edit:
        """Finalise a chunk: drop the markers and fold it into committed text."""

        def mutate() -> None:
            self.committed = append_chunk(self.committed, text)
            self.provisional = ""

        return self._transition(mutate)

    def abort(self) -> Edit:
        """Discard provisional text, leaving committed text untouched.

        Committed text is deliberately kept: it is already final, and silently
        deleting text the user watched settle is worse than a partial dictation.
        """

        def mutate() -> None:
            self.provisional = ""

        return self._transition(mutate)

    def clear_all(self) -> Edit:
        """Remove everything this program typed, committed included.

        Only for a dictation abandoned before any commit -- notably a press that
        never clears the hold threshold.
        """

        def mutate() -> None:
            self.committed = ""
            self.provisional = ""

        return self._transition(mutate)

    def reset(self) -> None:
        """Forget all state without emitting an edit (start of a new dictation)."""
        self.committed = ""
        self.provisional = ""
