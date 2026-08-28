"""Deterministic text post-processing.

Everything here is reviewable and predictable by construction. No model, no
rewriting, no judgement about what the speaker "meant" -- see the verbatim rule
in PLAN.md. The only transformations are: newline flattening (a newline injected
into a terminal executes the line), filler-word removal driven by an explicit
user list, and joining committed chunks.
"""

from __future__ import annotations

import re

_NEWLINES = re.compile(r"\r\n|\r|\n")
_RUNS_OF_SPACE = re.compile(r"[ \t]+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")
_DOUBLED_COMMA = re.compile(r",(?:\s*,)+")
_COMMA_BEFORE_TERMINAL = re.compile(r",\s*([.!?])")
_LEADING_JUNK = re.compile(r"^[\s,;:]+")


def normalize_newlines(text: str) -> str:
    """Flatten every newline to a single space.

    A transcript has no legitimate reason to contain one, and sending Enter into
    a terminal executes whatever is on the line.
    """
    return _RUNS_OF_SPACE.sub(" ", _NEWLINES.sub(" ", text)).strip()


def _tidy(text: str) -> str:
    """Repair the spacing and punctuation that removal leaves behind."""
    text = _RUNS_OF_SPACE.sub(" ", text)
    text = _COMMA_BEFORE_TERMINAL.sub(r"\1", text)
    text = _DOUBLED_COMMA.sub(",", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _LEADING_JUNK.sub("", text)
    return text.strip()


def build_filler_pattern(fillers: list[str]) -> re.Pattern[str] | None:
    """Compile a filler list into one alternation, longest term first.

    Longest-first matters so a multi-word entry like "you know" wins over a
    single-word "know" that appears later in the list.
    """
    terms = [t.strip() for t in fillers if t and t.strip()]
    if not terms:
        return None
    terms.sort(key=len, reverse=True)
    alts = "|".join(re.escape(t) for t in terms)
    # (?<!\w) / (?!\w) rather than \b so terms with trailing punctuation work.
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)\s*,?", re.IGNORECASE)


def strip_fillers(text: str, fillers: list[str]) -> str:
    """Remove filler words, then repair the surrounding punctuation."""
    if not text:
        return text
    pattern = build_filler_pattern(fillers)
    if pattern is None:
        return text
    return _tidy(pattern.sub(" ", text))


def join_chunks(parts: list[str]) -> str:
    """Join committed chunks with a single space.

    No punctuation surgery at the seams -- deciding a period "should not" be
    there is rewriting. See "seam artifacts" in PLAN.md.
    """
    return " ".join(p.strip() for p in parts if p and p.strip())


def append_chunk(existing: str, addition: str) -> str:
    """Append one chunk to committed text, inserting a separator if needed."""
    addition = addition.strip()
    if not addition:
        return existing
    if not existing:
        return addition
    if existing.endswith((" ", "\t")):
        return existing + addition
    return existing + " " + addition
