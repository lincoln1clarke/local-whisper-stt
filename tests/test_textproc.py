"""Tests for deterministic text post-processing."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lwstt.core.textproc import (
    append_chunk,
    capitalize_standalone_i,
    build_filler_pattern,
    join_chunks,
    normalize_newlines,
    strip_fillers,
)

FILLERS = ["um", "uh", "erm", "you know"]


class TestNormalizeNewlines:
    @pytest.mark.parametrize("nl", ["\n", "\r\n", "\r"])
    def test_every_newline_style_becomes_a_space(self, nl):
        assert normalize_newlines(f"one{nl}two") == "one two"

    def test_runs_collapse(self):
        assert normalize_newlines("one\n\n\ntwo") == "one two"

    def test_mixed_whitespace_collapses(self):
        assert normalize_newlines("one \t\n  two") == "one two"

    def test_edges_are_trimmed(self):
        assert normalize_newlines("\n hello \n") == "hello"

    def test_plain_text_is_untouched(self):
        assert normalize_newlines("Hello, world.") == "Hello, world."

    def test_empty(self):
        assert normalize_newlines("") == ""

    @given(st.text())
    def test_never_leaves_a_newline(self, text):
        """Sending Enter into a terminal executes the line. Never allow one."""
        assert "\n" not in normalize_newlines(text)
        assert "\r" not in normalize_newlines(text)


class TestStripFillers:
    def test_removes_filler_with_its_comma(self):
        assert strip_fillers("So, um, I think", FILLERS) == "So, I think"

    def test_removes_leading_filler(self):
        assert strip_fillers("Um, well, yes", FILLERS) == "well, yes"

    def test_removes_trailing_filler_before_a_period(self):
        assert strip_fillers("I think, um.", FILLERS) == "I think."

    def test_removes_several(self):
        assert strip_fillers("Um, well, uh, yes", FILLERS) == "well, yes"

    def test_bare_filler_without_punctuation(self):
        assert strip_fillers("um I think", FILLERS) == "I think"

    def test_multiword_filler(self):
        assert strip_fillers("It is, you know, fine", FILLERS) == "It is, fine"

    def test_respects_word_boundaries(self):
        assert strip_fillers("umbrella", FILLERS) == "umbrella"
        assert strip_fillers("a humdrum uhlan", FILLERS) == "a humdrum uhlan"

    def test_is_case_insensitive(self):
        assert strip_fillers("UM, yes", FILLERS) == "yes"
        assert strip_fillers("Uh, yes", FILLERS) == "yes"

    def test_empty_filler_list_is_a_noop(self):
        assert strip_fillers("So, um, I think", []) == "So, um, I think"

    def test_blank_entries_are_ignored(self):
        assert strip_fillers("hello", ["", "   "]) == "hello"

    def test_all_filler_yields_empty(self):
        assert strip_fillers("um uh um", FILLERS) == ""

    def test_empty_input(self):
        assert strip_fillers("", FILLERS) == ""

    def test_longest_match_wins(self):
        """A multi-word entry must beat a single word appearing later."""
        assert strip_fillers("It is you know fine", ["know", "you know"]) == "It is fine"

    def test_regex_metacharacters_in_a_filler_are_literal(self):
        assert strip_fillers("a c++ b", ["c++"]) == "a b"

    def test_does_not_touch_real_words_that_contain_fillers(self):
        text = "The summary uses numbers"
        assert strip_fillers(text, FILLERS) == text

    @given(st.text(max_size=60))
    def test_never_lengthens_the_text(self, text):
        assert len(strip_fillers(text, FILLERS)) <= len(text)

    @given(st.text(max_size=60))
    def test_never_introduces_a_newline(self, text):
        assert "\n" not in strip_fillers(normalize_newlines(text), FILLERS)


class TestBuildFillerPattern:
    def test_none_for_empty(self):
        assert build_filler_pattern([]) is None
        assert build_filler_pattern(["", "  "]) is None

    def test_orders_longest_first(self):
        """Python alternation is first-match, so longer terms must come first."""
        pattern = build_filler_pattern(["a", "aaa", "aa"])
        assert pattern is not None
        positions = [pattern.pattern.index(t) for t in ("aaa", "aa", "a")]
        assert positions == sorted(positions)

    def test_longer_alternative_matches_in_full(self):
        pattern = build_filler_pattern(["know", "you know"])
        assert pattern is not None
        assert pattern.search("you know").group(0).strip() == "you know"


class TestJoinChunks:
    def test_joins_with_single_spaces(self):
        assert join_chunks(["One.", "Two."]) == "One. Two."

    def test_drops_empties(self):
        assert join_chunks(["One.", "", "  ", "Two."]) == "One. Two."

    def test_trims_each_part(self):
        assert join_chunks(["  One.  ", "  Two."]) == "One. Two."

    def test_empty_list(self):
        assert join_chunks([]) == ""

    def test_does_not_repair_seam_punctuation(self):
        """Deciding a period should not be there is rewriting. See PLAN.md."""
        assert join_chunks(["I went to the store.", "And then I left."]) == (
            "I went to the store. And then I left."
        )


class TestAppendChunk:
    def test_appends_with_a_space(self):
        assert append_chunk("One.", "Two.") == "One. Two."

    def test_first_chunk_has_no_leading_space(self):
        assert append_chunk("", "One.") == "One."

    def test_does_not_double_the_separator(self):
        assert append_chunk("One. ", "Two.") == "One. Two."

    def test_empty_addition_changes_nothing(self):
        assert append_chunk("One.", "") == "One."
        assert append_chunk("One.", "   ") == "One."

    def test_addition_is_trimmed(self):
        assert append_chunk("One.", "  Two.  ") == "One. Two."

    @given(st.lists(st.text(max_size=20), max_size=10))
    def test_matches_join_chunks(self, parts):
        accumulated = ""
        for part in parts:
            accumulated = append_chunk(accumulated, part)
        assert accumulated == join_chunks(parts)


class TestCapitalizeStandaloneI:
    def test_capitalises_a_lone_i(self):
        assert capitalize_standalone_i("i think i am right") == "I think I am right"

    def test_handles_contractions(self):
        assert capitalize_standalone_i("i'm here and i'll wait") == "I'm here and I'll wait"

    def test_leaves_letters_inside_words_alone(self):
        for text in ("hi there", "wi-fi is fine", "the ninth item", "big"):
            assert capitalize_standalone_i(text) == text

    def test_leaves_ie_alone(self):
        assert capitalize_standalone_i("that is i.e. an example") == "that is i.e. an example"

    def test_already_capital_is_untouched(self):
        assert capitalize_standalone_i("I am fine") == "I am fine"

    def test_start_and_end_of_string(self):
        assert capitalize_standalone_i("i") == "I"
        assert capitalize_standalone_i("so am i") == "so am I"

    def test_adjacent_punctuation(self):
        assert capitalize_standalone_i("yes, i said (i did).") == "yes, I said (I did)."

    def test_the_imaginary_unit_is_the_accepted_casualty(self):
        """Documented trade: a bare mathematical i is capitalised too."""
        assert capitalize_standalone_i("the value of i") == "the value of I"

    def test_empty(self):
        assert capitalize_standalone_i("") == ""

    @given(st.text(max_size=80))
    def test_never_changes_length(self, text):
        assert len(capitalize_standalone_i(text)) == len(text)

    @given(st.text(max_size=80))
    def test_only_ever_changes_the_letter_i(self, text):
        result = capitalize_standalone_i(text)
        for before, after in zip(text, result):
            if before != after:
                assert before == "i" and after == "I"
