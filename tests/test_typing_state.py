"""Tests for the on-screen typing state machine.

These simulate a target application by applying every emitted edit to a plain
string, then assert the result matches what the state machine believes is on
screen. If those ever diverge in real use, text gets destroyed -- so the
simulation is the point, not an extra.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lwstt.core.diff import apply_edit
from lwstt.core.typing_state import TypingState


class Screen:
    """Stand-in for the focused text field."""

    def __init__(self) -> None:
        self.text = ""

    def apply(self, edit) -> None:
        self.text = apply_edit(self.text, edit)


@pytest.fixture
def pair():
    return TypingState(), Screen()


class TestProvisional:
    def test_first_preview_is_wrapped(self, pair):
        state, screen = pair
        screen.apply(state.set_provisional("hello"))
        assert screen.text == "~hello~"

    def test_growing_preview_stays_wrapped(self, pair):
        state, screen = pair
        screen.apply(state.set_provisional("hello"))
        screen.apply(state.set_provisional("hello there"))
        assert screen.text == "~hello there~"

    def test_growing_preview_is_cheap(self, pair):
        state, _ = pair
        state.set_provisional("I went to the sto")
        edit = state.set_provisional("I went to the store")
        assert edit.backspaces == 1, "only the closing marker should be rewritten"

    def test_revising_preview(self, pair):
        state, screen = pair
        screen.apply(state.set_provisional("the cat"))
        screen.apply(state.set_provisional("the car"))
        assert screen.text == "~the car~"

    def test_shrinking_preview(self, pair):
        state, screen = pair
        screen.apply(state.set_provisional("hello there"))
        screen.apply(state.set_provisional("hello"))
        assert screen.text == "~hello~"

    def test_empty_preview_shows_no_markers(self, pair):
        state, screen = pair
        screen.apply(state.set_provisional("hello"))
        screen.apply(state.set_provisional(""))
        assert screen.text == ""

    def test_setting_same_preview_is_a_noop(self, pair):
        state, _ = pair
        state.set_provisional("hello")
        assert state.set_provisional("hello").is_noop


class TestCommit:
    def test_commit_removes_markers(self, pair):
        state, screen = pair
        screen.apply(state.set_provisional("helo world"))
        screen.apply(state.commit("Hello world."))
        assert screen.text == "Hello world."
        assert state.provisional == ""

    def test_commit_without_preview(self, pair):
        state, screen = pair
        screen.apply(state.commit("Hello."))
        assert screen.text == "Hello."

    def test_second_chunk_is_space_separated(self, pair):
        state, screen = pair
        screen.apply(state.commit("Hello world."))
        screen.apply(state.commit("How are you?"))
        assert screen.text == "Hello world. How are you?"

    def test_committed_text_is_never_rewritten(self, pair):
        state, screen = pair
        screen.apply(state.commit("First chunk."))
        committed_len = len(screen.text)
        for preview in ("se", "sec", "second", "second chunk"):
            edit = state.set_provisional(preview)
            assert edit.backspaces <= len(state.wrapped_provisional()) + 2
            screen.apply(edit)
        assert screen.text.startswith("First chunk.")
        assert screen.text[:committed_len] == "First chunk."

    def test_backspace_radius_stays_bounded_over_a_long_dictation(self, pair):
        """The property that makes a 45-minute dictation safe."""
        state, screen = pair
        worst = 0
        for i in range(60):
            for preview in ("a", "a phrase", "a phrase here"):
                edit = state.set_provisional(preview)
                worst = max(worst, edit.backspaces)
                screen.apply(edit)
            edit = state.commit(f"Sentence number {i}.")
            worst = max(worst, edit.backspaces)
            screen.apply(edit)
        assert len(screen.text) > 1000
        assert worst < 30, f"largest rewrite was {worst} characters"

    def test_empty_commit_is_ignored(self, pair):
        state, screen = pair
        screen.apply(state.commit("Hello."))
        screen.apply(state.commit(""))
        assert screen.text == "Hello."


class TestAbort:
    def test_abort_removes_provisional_only(self, pair):
        state, screen = pair
        screen.apply(state.commit("Committed."))
        screen.apply(state.set_provisional("provisional"))
        screen.apply(state.abort())
        assert screen.text == "Committed."

    def test_abort_with_nothing_provisional_is_a_noop(self, pair):
        state, _ = pair
        state.commit("Committed.")
        assert state.abort().is_noop

    def test_clear_all_removes_everything(self, pair):
        state, screen = pair
        screen.apply(state.commit("Committed."))
        screen.apply(state.set_provisional("provisional"))
        screen.apply(state.clear_all())
        assert screen.text == ""

    def test_reset_emits_nothing(self, pair):
        state, screen = pair
        screen.apply(state.set_provisional("hello"))
        state.reset()
        assert state.on_screen() == ""
        assert screen.text == "~hello~", "reset must not touch the document"


class TestCustomMarkers:
    @pytest.mark.parametrize("marker", ["~", "<>", "›", "¦", "**"])
    def test_markers_round_trip(self, marker):
        state = TypingState(marker, marker)
        screen = Screen()
        screen.apply(state.set_provisional("hello"))
        assert screen.text == f"{marker}hello{marker}"
        screen.apply(state.commit("Hello."))
        assert screen.text == "Hello."

    def test_asymmetric_markers(self):
        state = TypingState("[[", "]]")
        screen = Screen()
        screen.apply(state.set_provisional("x"))
        assert screen.text == "[[x]]"

    def test_empty_markers_still_work(self):
        state = TypingState("", "")
        screen = Screen()
        screen.apply(state.set_provisional("hello"))
        assert screen.text == "hello"
        screen.apply(state.commit("Hello."))
        assert screen.text == "Hello."


PREVIEWS = st.lists(st.text(max_size=25), max_size=12)


class TestProperties:
    @given(PREVIEWS, st.lists(st.text(max_size=25), max_size=5))
    def test_screen_always_matches_belief(self, previews, commits):
        """The core invariant: what we think is on screen is what is on screen."""
        state, screen = TypingState(), Screen()
        for text in previews:
            screen.apply(state.set_provisional(text))
            assert screen.text == state.on_screen()
        for text in commits:
            screen.apply(state.commit(text))
            assert screen.text == state.on_screen()

    @given(PREVIEWS)
    def test_abort_always_returns_to_committed(self, previews):
        state, screen = TypingState(), Screen()
        screen.apply(state.commit("Base."))
        for text in previews:
            screen.apply(state.set_provisional(text))
        screen.apply(state.abort())
        assert screen.text == "Base."

    @given(PREVIEWS)
    def test_provisional_never_damages_committed_text(self, previews):
        state, screen = TypingState(), Screen()
        screen.apply(state.commit("Immutable prefix."))
        for text in previews:
            screen.apply(state.set_provisional(text))
            assert screen.text.startswith("Immutable prefix.")
