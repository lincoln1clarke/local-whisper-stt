"""Markdown word-list parsing.

vocabulary.md and filler.md are bullet lists. Lines starting with - or * are
entries; everything else is free-form commentary, so the files document
themselves. Order in vocabulary.md is priority.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

_BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
# Strip surrounding markdown emphasis so "- **Hopf**" yields "Hopf".
_EMPHASIS = re.compile(r"^(\*{1,3}|_{1,3}|`)(.*?)\1$")


def parse_markdown_list(text: str) -> list[str]:
    """Extract bullet entries, in file order, skipping commentary."""
    entries: list[str] = []
    for line in text.splitlines():
        match = _BULLET.match(line)
        if not match:
            continue
        item = match.group(1).strip()
        emphasis = _EMPHASIS.match(item)
        if emphasis:
            item = emphasis.group(2).strip()
        if item:
            entries.append(item)
    return entries


def load_list(path: str | Path) -> list[str]:
    """Read a word list. A missing or unreadable file is simply empty."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        return parse_markdown_list(p.read_text(encoding="utf-8"))
    except OSError:
        return []


def approx_token_count(text: str) -> int:
    """Rough token estimate used when no real tokenizer is available.

    The worker injects the model's actual tokenizer; this keeps core free of
    any ML dependency and gives tests something deterministic.
    """
    return max(1, (len(text) + 3) // 4)


def fit_to_budget(
    terms: list[str],
    max_tokens: int,
    count_tokens: Callable[[str], int] = approx_token_count,
    separator: str = ", ",
) -> list[str]:
    """Take terms in priority order until the token budget is reached.

    Stops at the first term that does not fit rather than skipping it to squeeze
    in later ones. That keeps the rule legible for a hand-edited file: everything
    above the cut is in, everything below is out.

    The **assembled prompt** is measured, not the sum of the individual terms.
    Tokenizers add special tokens to every call and merge across boundaries, so
    per-term costs do not add up to the cost of the joined string -- measuring
    term by term charged roughly two and a half times the real price and
    truncated the list far earlier than the budget required.
    """
    if max_tokens <= 0:
        return []
    kept: list[str] = []
    for term in terms:
        candidate = separator.join([*kept, term])
        if count_tokens(candidate) > max_tokens:
            break
        kept.append(term)
    return kept


def build_prompt(terms: list[str], separator: str = ", ") -> str:
    """Render the kept vocabulary terms into a prompt string."""
    return separator.join(terms)
