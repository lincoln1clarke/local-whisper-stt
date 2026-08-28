"""Tests for the minimal-edit computation.

This is the single most safety-relevant module in the project: every edit it
produces is executed as real backspaces against whatever document has focus. An
off-by-one here deletes the user's work.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from lwstt.core.diff import Edit, apply_edit, common_prefix_len, diff_edit

# Text likely to appear in a transcript, plus the markers and characters that
# have burned us before (auto-pairing, markdown, shell metacharacters).
TEXT = st.text(
    alphabet=st.sampled_from(list("abcdeABCDE .,!?~<>`'\"()[]{}|\\/$#\n\t") + ["é", "—", "🙂"]),
    max_size=40,
)


class TestCommonPrefixLen:
    def test_identical(self):
        assert common_prefix_len("hello", "hello") == 5

    def test_disjoint(self):
        assert common_prefix_len("abc", "xyz") == 0

    def test_partial(self):
        assert common_prefix_len("hello world", "hello there") == 6

    def test_empty_either_side(self):
        assert common_prefix_len("", "abc") == 0
        assert common_prefix_len("abc", "") == 0
        assert common_prefix_len("", "") == 0

    def test_one_is_prefix_of_other(self):
        assert common_prefix_len("ab", "abcdef") == 2
        assert common_prefix_len("abcdef", "ab") == 2

    def test_case_is_significant(self):
        assert common_prefix_len("Hello", "hello") == 0


class TestDiffEdit:
    def test_noop(self):
        edit = diff_edit("hello", "hello")
        assert edit == Edit(0, "")
        assert edit.is_noop

    def test_pure_append_costs_no_backspaces(self):
        assert diff_edit("hello", "hello world") == Edit(0, " world")

    def test_pure_truncation_types_nothing(self):
        assert diff_edit("hello world", "hello") == Edit(6, "")

    def test_tail_revision(self):
        assert diff_edit("the cat", "the car") == Edit(1, "r")

    def test_complete_replacement(self):
        assert diff_edit("abc", "xyz") == Edit(3, "xyz")

    def test_from_empty(self):
        assert diff_edit("", "hello") == Edit(0, "hello")

    def test_to_empty(self):
        assert diff_edit("hello", "") == Edit(5, "")

    def test_marker_move_is_cheap_on_append(self):
        # The whole point of diffing rather than retyping: growing the
        # provisional text by one character must not rewrite the marker.
        edit = diff_edit("~abc~", "~abcd~")
        assert edit == Edit(1, "d~")
        assert edit.keystrokes == 3

    def test_last_word_revision_is_cheap(self):
        old = "~I went to the stir~"
        new = "~I went to the store~"
        edit = diff_edit(old, new)
        assert edit.backspaces <= 4, "revising one word should not retype the phrase"


class TestApplyEdit:
    def test_applies(self):
        assert apply_edit("hello", Edit(1, "p!")) == "hellp!"

    def test_overlong_backspace_clamps(self):
        # Mirrors a text field at position zero, and a terminal where readline
        # refuses to eat the prompt.
        assert apply_edit("ab", Edit(10, "x")) == "x"

    def test_negative_backspaces_rejected(self):
        import pytest

        with pytest.raises(ValueError):
            apply_edit("ab", Edit(-1, ""))


class TestProperties:
    @given(TEXT, TEXT)
    def test_edit_always_produces_the_target(self, old, new):
        """The invariant everything else depends on."""
        assert apply_edit(old, diff_edit(old, new)) == new

    @given(TEXT, TEXT)
    def test_never_deletes_more_than_exists(self, old, new):
        """A backspace count exceeding what we typed would eat the user's text."""
        assert diff_edit(old, new).backspaces <= len(old)

    @given(TEXT, TEXT)
    def test_never_costs_more_than_full_retype(self, old, new):
        edit = diff_edit(old, new)
        assert edit.keystrokes <= len(old) + len(new)

    @given(TEXT, TEXT)
    def test_noop_exactly_when_equal(self, old, new):
        assert diff_edit(old, new).is_noop == (old == new)

    @given(TEXT, TEXT)
    def test_shared_prefix_is_never_retyped(self, old, new):
        """Committed text sits in the shared prefix; it must never be touched."""
        edit = diff_edit(old, new)
        untouched = len(old) - edit.backspaces
        assert old[:untouched] == new[:untouched]

    @given(TEXT, TEXT)
    def test_append_only_growth_never_backspaces(self, base, suffix):
        assert diff_edit(base, base + suffix).backspaces == 0
