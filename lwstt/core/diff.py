"""Minimal-edit computation for in-place retyping.

The whole output design rests on one invariant: N characters typed equals N
characters present on screen. Everything here is deliberately simple so that
invariant is easy to reason about.

Only a common-prefix diff is used, not a full edit-distance diff. Whisper
previews revise the tail of an utterance, so a prefix diff produces near-optimal
edits, and unlike a general diff it can only ever touch a contiguous suffix --
which is exactly the property that makes it safe to drive a keyboard with.
"""

from __future__ import annotations

from typing import NamedTuple


class Edit(NamedTuple):
    """How to turn one on-screen string into another."""

    backspaces: int
    text: str

    @property
    def is_noop(self) -> bool:
        return self.backspaces == 0 and not self.text

    @property
    def keystrokes(self) -> int:
        """Total input events this edit costs. Useful for flicker assertions."""
        return self.backspaces + len(self.text)


def common_prefix_len(a: str, b: str) -> int:
    """Number of leading characters ``a`` and ``b`` share."""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def diff_edit(old: str, new: str) -> Edit:
    """Backspaces then text that turn ``old`` into ``new``."""
    n = common_prefix_len(old, new)
    return Edit(backspaces=len(old) - n, text=new[n:])


def apply_edit(old: str, edit: Edit) -> str:
    """Reference implementation of what the target application will do.

    Used by tests to assert that an edit actually produces the intended string.
    Backspacing further than the text length is clamped, mirroring a text field
    at position zero (and a terminal, where readline refuses to eat the prompt).
    """
    if edit.backspaces < 0:
        raise ValueError("backspaces must not be negative")
    kept = old[: len(old) - edit.backspaces] if edit.backspaces <= len(old) else ""
    return kept + edit.text
