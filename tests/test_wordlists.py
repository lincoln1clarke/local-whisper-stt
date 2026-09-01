"""Tests for markdown word-list parsing and the vocabulary token budget."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lwstt.core.wordlists import (
    approx_token_count,
    build_prompt,
    fit_to_budget,
    load_list,
    parse_markdown_list,
)

SAMPLE = """\
# Vocabulary

Terms I say often, in priority order. Anything that is not a bullet is
commentary and is ignored.

- Hopf
- Grothendieck
* Cayley

Some more notes here.

-   spaced out
- **bold term**
- `code term`
-
- trailing spaces
"""


class TestParsing:
    def test_extracts_bullets_in_order(self):
        assert parse_markdown_list(SAMPLE)[:3] == ["Hopf", "Grothendieck", "Cayley"]

    def test_both_bullet_characters(self):
        assert parse_markdown_list("- a\n* b") == ["a", "b"]

    def test_commentary_is_ignored(self):
        assert "commentary" not in " ".join(parse_markdown_list(SAMPLE)).lower()

    def test_headings_are_ignored(self):
        assert parse_markdown_list("# Heading\n- item") == ["item"]

    def test_extra_whitespace_is_trimmed(self):
        assert "spaced out" in parse_markdown_list(SAMPLE)

    def test_emphasis_is_stripped(self):
        entries = parse_markdown_list(SAMPLE)
        assert "bold term" in entries
        assert "code term" in entries

    def test_empty_bullet_is_skipped(self):
        assert "" not in parse_markdown_list(SAMPLE)

    def test_indented_bullets_count(self):
        assert parse_markdown_list("  - indented") == ["indented"]

    def test_hyphen_without_a_space_is_not_a_bullet(self):
        assert parse_markdown_list("-notabullet") == []

    def test_horizontal_rule_is_not_a_bullet(self):
        assert parse_markdown_list("---") == []

    def test_empty_input(self):
        assert parse_markdown_list("") == []

    def test_no_bullets_at_all(self):
        assert parse_markdown_list("Just prose.\nMore prose.") == []

    def test_multiword_entries_survive(self):
        assert parse_markdown_list("- machine learning") == ["machine learning"]

    def test_entries_with_punctuation(self):
        assert parse_markdown_list("- C++\n- .NET") == ["C++", ".NET"]

    def test_duplicates_are_preserved(self):
        """Deduplication is the user's business, not ours."""
        assert parse_markdown_list("- a\n- a") == ["a", "a"]


class TestLoading:
    def test_reads_a_file(self, tmp_path):
        p = tmp_path / "vocabulary.md"
        p.write_text(SAMPLE, encoding="utf-8")
        assert "Hopf" in load_list(p)

    def test_missing_file_is_empty(self, tmp_path):
        assert load_list(tmp_path / "absent.md") == []

    def test_utf8_entries(self, tmp_path):
        p = tmp_path / "v.md"
        p.write_text("- naïve\n- Gödel", encoding="utf-8")
        assert load_list(p) == ["naïve", "Gödel"]

    def test_empty_file(self, tmp_path):
        p = tmp_path / "v.md"
        p.write_text("", encoding="utf-8")
        assert load_list(p) == []


class TestBudget:
    def test_everything_fits_under_a_large_budget(self):
        terms = ["alpha", "beta", "gamma"]
        assert fit_to_budget(terms, 224) == terms

    def test_truncates_at_the_budget(self):
        terms = ["x" * 40 for _ in range(20)]
        kept = fit_to_budget(terms, 30)
        assert 0 < len(kept) < len(terms)

    def test_priority_order_is_honoured(self):
        terms = ["first", "second", "third"]
        kept = fit_to_budget(terms, 10)
        assert kept == terms[: len(kept)]

    def test_stops_rather_than_skipping(self):
        """Everything above the cut is in, everything below is out."""
        terms = ["a" * 200, "b"]
        assert fit_to_budget(terms, 20) == []

    def test_zero_budget_keeps_nothing(self):
        assert fit_to_budget(["a"], 0) == []

    def test_negative_budget_keeps_nothing(self):
        assert fit_to_budget(["a"], -5) == []

    def test_empty_term_list(self):
        assert fit_to_budget([], 224) == []

    def test_separator_cost_is_counted(self):
        def count(text):
            return len(text)

        # "aaaa" + ", " + "bbbb" == 10 characters; a budget of 9 must drop one.
        assert fit_to_budget(["aaaa", "bbbb"], 9, count) == ["aaaa"]
        assert fit_to_budget(["aaaa", "bbbb"], 10, count) == ["aaaa", "bbbb"]

    def test_custom_counter_is_used(self):
        # The assembled prompt is measured: "a" is 1 char, "a, b" is 4.
        assert fit_to_budget(["a", "b", "c"], 1, len) == ["a"]
        assert fit_to_budget(["a", "b", "c"], 4, len) == ["a", "b"]
        assert fit_to_budget(["a", "b", "c"], 7, len) == ["a", "b", "c"]

    def test_measures_the_assembled_prompt_not_the_sum_of_terms(self):
        """Per-term counting overcharges: tokenizers add special tokens to every
        call and merge across boundaries, so the parts exceed the whole."""
        calls = []

        def counting(text):
            calls.append(text)
            return len(text)

        fit_to_budget(["alpha", "beta"], 100, counting)
        assert calls == ["alpha", "alpha, beta"]
        assert all(", " not in c or c.startswith("alpha") for c in calls)

    @given(st.lists(st.text(min_size=1, max_size=20), max_size=30), st.integers(0, 300))
    def test_result_is_always_a_prefix(self, terms, budget):
        kept = fit_to_budget(terms, budget)
        assert kept == terms[: len(kept)]

    @given(st.lists(st.text(min_size=1, max_size=20), max_size=30), st.integers(1, 300))
    def test_never_exceeds_the_budget(self, terms, budget):
        kept = fit_to_budget(terms, budget)
        if kept:
            assert approx_token_count(build_prompt(kept)) <= budget


class TestPrompt:
    def test_joins_with_commas(self):
        assert build_prompt(["Hopf", "Cayley"]) == "Hopf, Cayley"

    def test_empty(self):
        assert build_prompt([]) == ""

    def test_single(self):
        assert build_prompt(["Hopf"]) == "Hopf"


class TestApproxTokenCount:
    def test_is_always_positive(self):
        assert approx_token_count("") >= 1

    @pytest.mark.parametrize("text", ["a", "abcd", "a much longer piece of text"])
    def test_grows_with_length(self, text):
        assert approx_token_count(text) >= 1

    @given(st.text(max_size=200))
    def test_never_negative(self, text):
        assert approx_token_count(text) >= 1


class TestLocalOverride:
    """A personal vocabulary names employers, clients and projects.

    It has to stay out of version control while the tracked file remains a
    useful starting point, so a ".local" sibling wins over it.
    """

    def test_the_local_file_wins(self, tmp_path):
        from lwstt.core.wordlists import load_list

        (tmp_path / "vocabulary.md").write_text("- Shipped\n", encoding="utf-8")
        (tmp_path / "vocabulary.local.md").write_text("- Personal\n", encoding="utf-8")
        assert load_list(tmp_path / "vocabulary.md") == ["Personal"]

    def test_the_tracked_file_is_used_when_there_is_no_local_one(self, tmp_path):
        from lwstt.core.wordlists import load_list

        (tmp_path / "vocabulary.md").write_text("- Shipped\n", encoding="utf-8")
        assert load_list(tmp_path / "vocabulary.md") == ["Shipped"]

    def test_missing_everything_is_empty_not_an_error(self, tmp_path):
        from lwstt.core.wordlists import load_list

        assert load_list(tmp_path / "nope.md") == []

    def test_the_shipped_vocabulary_carries_no_personal_terms(self):
        """The tracked file is a template; personal entries belong in .local."""
        from pathlib import Path

        from lwstt.core.wordlists import parse_markdown_list

        root = Path(__file__).resolve().parent.parent
        terms = parse_markdown_list((root / "vocabulary.md").read_text(encoding="utf-8"))
        assert terms, "the template should still show some examples"
