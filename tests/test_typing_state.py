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

from lwstt.core.diff import Edit, apply_edit
from lwstt.core.typing_state import TypingState


class Screen:
    """Stand-in for the focused text field."""

    def __init__(self) -> None:
        self.text = ""

    def apply(self, edit) -> None:
        self.text = apply_edit(self.text, edit)


@pytest.fixture
def pair():
    # leading_space off here: these tests are about the marker and diff
    # mechanics. The space itself has its own class below.
    return TypingState(leading_space=False), Screen()


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
        state = TypingState(marker, marker, leading_space=False)
        screen = Screen()
        screen.apply(state.set_provisional("hello"))
        assert screen.text == f"{marker}hello{marker}"
        screen.apply(state.commit("Hello."))
        assert screen.text == "Hello."

    def test_asymmetric_markers(self):
        state = TypingState("[[", "]]", leading_space=False)
        screen = Screen()
        screen.apply(state.set_provisional("x"))
        assert screen.text == "[[x]]"

    def test_empty_markers_still_work(self):
        state = TypingState("", "", leading_space=False)
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
        state, screen = TypingState(leading_space=False), Screen()
        for text in previews:
            screen.apply(state.set_provisional(text))
            assert screen.text == state.on_screen()
        for text in commits:
            screen.apply(state.commit(text))
            assert screen.text == state.on_screen()

    @given(PREVIEWS)
    def test_abort_always_returns_to_committed(self, previews):
        state, screen = TypingState(leading_space=False), Screen()
        screen.apply(state.commit("Base."))
        for text in previews:
            screen.apply(state.set_provisional(text))
        screen.apply(state.abort())
        assert screen.text == "Base."

    @given(PREVIEWS)
    def test_provisional_never_damages_committed_text(self, previews):
        state, screen = TypingState(leading_space=False), Screen()
        screen.apply(state.commit("Immutable prefix."))
        for text in previews:
            screen.apply(state.set_provisional(text))
            assert screen.text.startswith("Immutable prefix.")


class TestLeadingSpace:
    """Dictation usually starts where a caret already sits at the end of a
    word, and there is no way to read the target to find out."""

    def test_a_space_precedes_the_first_preview(self):
        state, screen = TypingState(leading_space=True), Screen()
        screen.apply(state.set_provisional("hello"))
        assert screen.text == " ~hello~"

    def test_a_space_precedes_a_commit_with_no_preview(self):
        state, screen = TypingState(leading_space=True), Screen()
        screen.apply(state.commit("Hello."))
        assert screen.text == " Hello."

    def test_only_one_space_across_several_chunks(self):
        state, screen = TypingState(leading_space=True), Screen()
        screen.apply(state.commit("One."))
        screen.apply(state.commit("Two."))
        assert screen.text == " One. Two."

    def test_nothing_is_typed_before_there_is_content(self):
        state, screen = TypingState(leading_space=True), Screen()
        assert state.on_screen() == ""
        assert state.set_provisional("").is_noop
        assert screen.text == ""

    def test_abort_removes_the_space_too(self):
        state, screen = TypingState(leading_space=True), Screen()
        screen.apply(state.set_provisional("hello"))
        screen.apply(state.abort())
        assert screen.text == "", "a cancelled dictation must leave no stray space"

    def test_disabled_leaves_no_space(self):
        state, screen = TypingState(leading_space=False), Screen()
        screen.apply(state.set_provisional("hello"))
        assert screen.text == "~hello~"


class TestPreviewEdit:
    """peek-without-mutating, used to decide whether an update is worth the flicker."""

    def test_matches_what_set_provisional_would_do(self):
        for leading in (True, False):
            state = TypingState(leading_space=leading)
            state.set_provisional("the cat")
            peeked = state.preview_edit("the car")
            applied = state.set_provisional("the car")
            assert peeked == applied

    def test_does_not_mutate(self):
        state = TypingState(leading_space=False)
        state.set_provisional("hello")
        before = state.on_screen()
        state.preview_edit("something else entirely")
        assert state.on_screen() == before

    def test_growth_only_rewrites_the_closing_marker(self):
        """Why the supervisor compares provisional text rather than this edit:
        even a pure append rewrites the trailing marker."""
        state = TypingState(leading_space=False)
        state.set_provisional("hello")
        edit = state.preview_edit("hello there")
        assert edit.backspaces == 1
        assert edit.text == " there~"

    def test_growth_is_far_cheaper_than_revision(self):
        state = TypingState(leading_space=False)
        state.set_provisional("I went to the store")
        growth = state.preview_edit("I went to the store today")
        revision = state.preview_edit("I want to the shop today")
        assert growth.keystrokes < revision.keystrokes

    def test_revision_costs_backspaces(self):
        state = TypingState(leading_space=False)
        state.set_provisional("the cat")
        assert state.preview_edit("the dog").backspaces > 0


class TestListeningMarkers:
    """Empty markers appear the moment the hold threshold passes, before any
    transcription exists. They confirm it is listening, and confirm the caret is
    somewhere that accepts text."""

    def test_markers_appear_with_no_text(self):
        state, screen = TypingState(leading_space=False), Screen()
        screen.apply(state.set_listening(True))
        assert screen.text == "~~"

    def test_leading_space_applies_to_them(self):
        state, screen = TypingState(leading_space=True), Screen()
        screen.apply(state.set_listening(True))
        assert screen.text == " ~~"

    def test_first_preview_fills_them_in(self):
        state, screen = TypingState(leading_space=False), Screen()
        screen.apply(state.set_listening(True))
        edit = state.set_provisional("hello")
        screen.apply(edit)
        assert screen.text == "~hello~"
        assert edit.backspaces == 1, "only the closing marker moves"

    def test_they_return_after_a_commit(self):
        state, screen = TypingState(leading_space=False), Screen()
        screen.apply(state.set_listening(True))
        screen.apply(state.set_provisional("hello"))
        screen.apply(state.commit("Hello."))
        assert screen.text == "Hello. ~~", "still listening after a chunk lands"

    def test_clearing_them_leaves_committed_text(self):
        state, screen = TypingState(leading_space=False), Screen()
        screen.apply(state.set_listening(True))
        screen.apply(state.commit("Hello."))
        screen.apply(state.set_listening(False))
        assert screen.text == "Hello."

    def test_abort_removes_them(self):
        state, screen = TypingState(leading_space=True), Screen()
        screen.apply(state.set_listening(True))
        screen.apply(state.abort())
        assert screen.text == ""

    def test_disabled_shows_nothing(self):
        state, screen = TypingState(leading_space=False), Screen()
        assert state.set_listening(False).is_noop
        assert screen.text == ""


class TestSpacingBetweenCommittedAndPreview:
    def test_preview_does_not_run_into_committed_text(self):
        state, screen = TypingState(leading_space=False), Screen()
        screen.apply(state.commit("First sentence."))
        screen.apply(state.set_provisional("second"))
        assert screen.text == "First sentence. ~second~"

    def test_committing_that_preview_keeps_one_space(self):
        state, screen = TypingState(leading_space=False), Screen()
        screen.apply(state.commit("First sentence."))
        screen.apply(state.set_provisional("second"))
        screen.apply(state.commit("Second sentence."))
        assert screen.text == "First sentence. Second sentence."


class TestCommitRetiringTheMarkers:
    def test_commit_keeps_the_markers_by_default(self):
        state = TypingState()
        state.set_listening(True)
        state.commit("One.")
        assert state.on_screen() == " One. ~~"

    def test_commit_can_retire_them_in_the_same_edit(self):
        state = TypingState()
        state.set_listening(True)
        before = state.on_screen()
        edit = state.commit("One.", keep_listening=False)
        assert apply_edit(before, edit) == " One."
        assert state.on_screen() == " One."
        assert state.commit("Two.", keep_listening=False) == Edit(0, " Two.")
